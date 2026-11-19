"""request/result codec for native cluster-label import.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import tempfile

from mif_pipeline.datasets.references import freeze_component_reference

from mif_pipeline.orchestration.batch_analysis_preflight import (
    MIF_PIPELINE_BATCH_RUNTIME_VERSION,
)
from mif_pipeline.orchestration.batch_artifacts import (
    ArtifactIdentityContract,
    ArtifactOrigin,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchNodeExecutionResult,
    validate_artifact_ref,
    validate_committed_artifact_authority_set,
)
from mif_pipeline.orchestration.phenotype_views import PreparedPhenotypeView
from mif_pipeline.orchestration.project_stage_plan import (
    ProjectStageTargetExecutionEvidence,
)
from mif_pipeline.orchestration.workflow import CurrentImageScientificInvocation
from mif_pipeline.pipeline_core.config import AnalysisConfig
from mif_pipeline.pipeline_core.stage_result import StageStatus
from mif_pipeline.qupath_runtime.scripts import QuPathScriptAsset

from ._common import (
    BatchPhenotypingError,
    canonical_bytes,
    digest,
    plain_json,
    read_bounded_file,
    sha256,
    text,
)
from .accepted_review import AcceptedClusterLabelReview
from .review_authority import (
    accepted_cluster_label_review_identity_contract,
    accepted_cluster_label_review_owning_identity,
)
from .application_records import (
    SEMANTIC_LIVE_BINDING_FIELDS,
    _freeze_mapping,
    ClusterLabelSectionApplication,
    PhenotypeHierarchyAttestation,
    SemanticRawSnapshot,
    ValidatedClusterLabelImportProjection,
    ValidatedPhenotypeHierarchyAttestationProjection,
    ValidatedSemanticRawInventoryProjection,
    phenotype_hierarchy_attestation_identity_contract,
    semantic_raw_inventory_identity_contract,
)
from .authority import (
    clustering_result_identity_contract,
    clustering_result_owning_identity,
)
from .codec import _loads_batch_clustering_result

CLUSTER_LABEL_IMPORT_RESULT_KIND = "cluster_label_import_result"
CLUSTER_LABEL_IMPORT_RESULT_SCHEMA_NAME = "roi-generation-manifest"
CLUSTER_LABEL_IMPORT_RESULT_SCHEMA_VERSION = 1
_PRODUCER_VERSION = "mif-pipeline-batch-cluster-label-import/v1"

_IDENTITY_FIELDS = (
    "sample_id",
    "slide_id",
    "project_entry_id",
    "canonical_image_id",
    "canonical_image_sha256",
    "section_id",
    "tissue_generation_id",
    "tissue_path_object_id",
    "tissue_roi_id",
    "segmentation_generation_id",
    "segmentation_population_id",
    "measurement_generation_id",
    "clustering_generation_sha256",
    "accepted_review_sha256",
    "composite_projection_sha256",
    "cell_ids_sha256",
    "cell_to_domain_membership_sha256",
    "phenotype_generation_id",
    "phenotype_view_artifact_sha256",
    "phenotype_view_lineage_sha256",
    "source_ref_sha256s",
    "saved_state_before_sha256",
    "saved_state_after_sha256",
    "saved_state_disposition",
    "execution_exchange_sha256",
    "validated_exchange_sha256",
    "generation_manifest_payload_sha256",
    "result_payload_sha256",
    "task10_execution_identity_sha256",
    "node_id",
    "attempt_id",
    "attempt_authorization_sha256",
    "input_artifact_set_sha256",
    "adapter_spec_sha256",
    "projection_target_sha256",
    "compatibility_signature_sha256",
    "request_codec_id",
    "request_codec_version",
)


# ==============================================================================
#                               Application codec
# ==============================================================================


def _projection(cls: type[object], **values: object) -> object:
    result = object.__new__(cls)
    for name in cls.__dataclass_fields__:  # type: ignore[attr-defined]
        object.__setattr__(result, name, values[name])
    return result


def _committed_authority(
    authority: BatchCommittedArtifactAuthority,
) -> tuple[BatchCommittedArtifactAuthority, BatchArtifactRef]:
    try:
        (validated,) = validate_committed_artifact_authority_set((authority,))
        return validated, validate_artifact_ref(validated.artifact_ref)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("artifact is not current committed authority") from exc


def decode_committed_clustering_result_for_application(
    authority: BatchCommittedArtifactAuthority,
    *,
    maximum_byte_count: int = 512 * 1024 * 1024,
) -> "BatchClusteringExecutionResult":
    """Reopen one committed graph for a current-population application join.

    The graph's complete source set remains bound by its owner identity.  This
    downstream decoder additionally joins the selected entry to a current
    measurement authority before any local-cell projection is authorized.
    """

    validated, artifact = _committed_authority(authority)
    contract = clustering_result_identity_contract()
    if (
        validated.producer_node_kind
        not in {"clustering_per_slide", "clustering_pooled"}
        or artifact.artifact_kind != "clustered_phenotype_result"
        or (artifact.schema_name, artifact.schema_version) != ("mif-batch-clustering-result", 1)
        or artifact.identity_contract != contract
        or artifact.owning_identity_contract_sha256 != contract.contract_sha256
        or artifact.origin is not ArtifactOrigin.INTERNAL_NODE
        or artifact.locator is None
        or artifact.sha256 is None
        or artifact.byte_count is None
    ):
        raise BatchPhenotypingError("clustering result lacks its committed owner")
    sources = tuple(artifact.owning_identity.get("source_authority_sha256s", ()))
    if sources != tuple(sorted(set(sources))) or not all(isinstance(item, str) for item in sources):
        raise BatchPhenotypingError("clustering result source identity is invalid")
    payload = read_bounded_file(
        artifact.locator,
        expected_byte_count=artifact.byte_count,
        maximum_byte_count=maximum_byte_count,
    )
    if hashlib.sha256(payload).hexdigest() != artifact.sha256:
        raise BatchPhenotypingError("clustering result bytes changed")
    result = _loads_batch_clustering_result(
        payload,
        expected_request_sha256=str(artifact.owning_identity.get("request_sha256")),
        expected_source_authority_sha256s=sources,  # type: ignore[arg-type]
    )
    expected_kind = {
        "per_slide": "clustering_per_slide",
        "pooled": "clustering_pooled",
    }[result.mode.value]
    if validated.producer_node_kind != expected_kind or canonical_bytes(
        clustering_result_owning_identity(result)
    ) != canonical_bytes(artifact.owning_identity):
        raise BatchPhenotypingError("clustering result content differs from owner")
    return result


def decode_committed_accepted_cluster_label_review(
    authority: BatchCommittedArtifactAuthority,
    *,
    maximum_byte_count: int = 16 * 1024 * 1024,
) -> AcceptedClusterLabelReview:
    """Reopen accepted labels only from current state-issued artifact authority."""

    try:
        (validated,) = validate_committed_artifact_authority_set((authority,))
        artifact = validate_artifact_ref(validated.artifact_ref)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError(
            "accepted cluster-label review is not state-authenticated"
        ) from exc
    contract = accepted_cluster_label_review_identity_contract()
    if (
        validated.producer_node_kind != "cluster_label_review"
        or artifact.artifact_kind != "accepted_cluster_label_review"
        or (artifact.schema_name, artifact.schema_version)
        != ("mif-batch-accepted-cluster-label-review", 1)
        or artifact.identity_contract != contract
        or artifact.owning_identity_contract_sha256 != contract.contract_sha256
        or artifact.origin is not ArtifactOrigin.INTERNAL_NODE
        or artifact.locator is None
        or artifact.sha256 is None
        or artifact.byte_count is None
        or validated.execution_exchange_sha256 is not None
    ):
        raise BatchPhenotypingError(
            "accepted cluster-label review lacks its persisted owner contract"
        )
    payload = read_bounded_file(
        artifact.locator,
        expected_byte_count=artifact.byte_count,
        maximum_byte_count=maximum_byte_count,
    )
    if hashlib.sha256(payload).hexdigest() != artifact.sha256:
        raise BatchPhenotypingError("accepted cluster-label review bytes changed")
    try:
        import json

        decoded = json.loads(payload.decode("utf-8"))
        if canonical_bytes(decoded) != payload:
            raise BatchPhenotypingError("accepted cluster-label review is not canonical JSON")
        review = AcceptedClusterLabelReview.from_mapping(decoded)
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise BatchPhenotypingError("accepted cluster-label review content is invalid") from exc
    if (
        canonical_bytes(artifact.owning_identity)
        != canonical_bytes(accepted_cluster_label_review_owning_identity(review))
        or tuple(item.sample_id for item in artifact.configuration_bindings)
        != review.participant_ids
    ):
        raise BatchPhenotypingError(
            "accepted cluster-label review identity differs from persisted content"
        )
    return review


def cluster_label_import_result_identity_contract() -> ArtifactIdentityContract:
    return ArtifactIdentityContract(
        contract_id="mif-batch-cluster-label-import-result-identity/v1",
        artifact_kind=CLUSTER_LABEL_IMPORT_RESULT_KIND,
        schema_name=CLUSTER_LABEL_IMPORT_RESULT_SCHEMA_NAME,
        schema_version=CLUSTER_LABEL_IMPORT_RESULT_SCHEMA_VERSION,
        required_fields=tuple(sorted((*_IDENTITY_FIELDS, "component_descriptor.path", "component_descriptor.sha256"))),
        optional_fields=(),
        required_runtime_names=("mif_pipeline", "qupath"),
    )


def decode_committed_cluster_label_import_result(
    authority: BatchCommittedArtifactAuthority,
) -> ValidatedClusterLabelImportProjection:
    """Decode only the compact result of the committed native import child."""

    validated, artifact = _committed_authority(authority)
    contract = cluster_label_import_result_identity_contract()
    identity = artifact.owning_identity
    if (
        validated.producer_node_kind != "clustering_label_import"
        or artifact.artifact_kind != CLUSTER_LABEL_IMPORT_RESULT_KIND
        or (artifact.schema_name, artifact.schema_version)
        != (
            CLUSTER_LABEL_IMPORT_RESULT_SCHEMA_NAME,
            CLUSTER_LABEL_IMPORT_RESULT_SCHEMA_VERSION,
        )
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
        or tuple(item.sample_id for item in artifact.configuration_bindings)
        != (identity["sample_id"],)
    ):
        raise BatchPhenotypingError("cluster-label import result owner is invalid")
    for field in _IDENTITY_FIELDS:
        value = identity[field]
        if field.endswith("_sha256"):
            sha256(value, field)
    for field in (
        "sample_id",
        "slide_id",
        "project_entry_id",
        "canonical_image_id",
        "section_id",
        "tissue_generation_id",
        "tissue_path_object_id",
        "tissue_roi_id",
        "segmentation_generation_id",
        "segmentation_population_id",
        "measurement_generation_id",
        "phenotype_generation_id",
        "node_id",
        "attempt_id",
        "request_codec_id",
        "request_codec_version",
    ):
        text(identity[field], field)
    freeze_component_reference(identity["component_descriptor"])
    source_refs = _canonical_ids(identity["source_ref_sha256s"], "source_ref_sha256s")
    if not source_refs:
        raise BatchPhenotypingError("cluster-label import source set is invalid")
    for source_ref in source_refs:
        sha256(source_ref, "source_ref_sha256s")
    saved_disposition = identity["saved_state_disposition"]
    expected_task10_identity = digest(
        {key: value for key, value in identity.items() if key != "task10_execution_identity_sha256"}
    )
    if (
        identity["task10_execution_identity_sha256"] != expected_task10_identity
        or identity["input_artifact_set_sha256"]
        != digest({"artifact_ref_sha256s": list(source_refs)})
        or identity["request_codec_id"] != QuPathScriptAsset.IMPORT_PYTHON_CELL_LABELS.value
        or identity["request_codec_version"] != "1"
        or saved_disposition not in {"authenticated_unchanged", "authenticated_changed"}
        or (
            (identity["saved_state_before_sha256"] == identity["saved_state_after_sha256"])
            != (saved_disposition == "authenticated_unchanged")
        )
        or artifact.producer_node_id != identity["node_id"]
        or artifact.producer_attempt_id != identity["attempt_id"]
        or artifact.producer_attempt_authorization_sha256
        != identity["attempt_authorization_sha256"]
        or artifact.compatibility_signature_sha256 != identity["compatibility_signature_sha256"]
    ):
        raise BatchPhenotypingError("cluster-label import execution identity differs from owner")
    result = _projection(
        ValidatedClusterLabelImportProjection,
        artifact_ref=artifact,
        state_head_sha256=validated.state_head_sha256,
        active_plan_sha256=validated.active_plan_sha256,
        terminal_record_sha256=validated.terminal_record_sha256,
        identity=identity,
    )
    assert isinstance(result, ValidatedClusterLabelImportProjection)
    return result


def _canonical_ids(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise BatchPhenotypingError(f"{field} must be an exact array")
    result = tuple(text(item, field) for item in value)
    if result != tuple(sorted(set(result))):
        raise BatchPhenotypingError(f"{field} must be sorted and unique")
    return result


def decode_committed_semantic_raw_inventory(
    authority: BatchCommittedArtifactAuthority,
    *,
    maximum_byte_count: int = 512 * 1024 * 1024,
) -> ValidatedSemanticRawInventoryProjection:
    """Reopen authenticated raw bytes and derive exact section hierarchy rows."""

    validated, artifact = _committed_authority(authority)
    contract = semantic_raw_inventory_identity_contract()
    identity = artifact.owning_identity
    if (
        validated.producer_node_kind != "semantic_raw_inventory_snapshot"
        or artifact.artifact_kind != "semantic_raw_inventory"
        or (artifact.schema_name, artifact.schema_version) != ("qupath-raw-objects", 1)
        or artifact.identity_contract != contract
        or artifact.owning_identity_contract_sha256 != contract.contract_sha256
        or artifact.origin is not ArtifactOrigin.INTERNAL_NODE
        or artifact.locator is None
        or artifact.sha256 is None
        or artifact.byte_count is None
        or artifact.sha256 != identity["raw_export_sha256"]
        or artifact.byte_count != identity["raw_export_byte_count"]
        or validated.execution_exchange_sha256 != identity["execution_exchange_sha256"]
        or artifact.scope.sample_id != identity["sample_id"]
        or artifact.scope.slide_id != identity["slide_id"]
        or artifact.scope.project_entry_id != identity["project_entry_id"]
    ):
        raise BatchPhenotypingError("semantic raw inventory owner is invalid")
    for field in (
        "sample_id",
        "project_entry_id",
        "slide_id",
        "canonical_image_id",
        "section_id",
        "tissue_generation_id",
        "segmentation_population_id",
        "phenotype_generation_id",
    ):
        try:
            text(identity[field], field)
        except BatchPhenotypingError as exc:
            raise BatchPhenotypingError("semantic raw inventory identity is invalid") from exc
    for field, value in identity.items():
        if field.endswith("_sha256"):
            sha256(value, field)
    if (
        type(identity["raw_export_byte_count"]) is not int
        or identity["raw_export_byte_count"] <= 0
        or tuple(item.sample_id for item in artifact.configuration_bindings)
        != (identity["sample_id"],)
    ):
        raise BatchPhenotypingError("semantic raw inventory identity is invalid")
    payload = read_bounded_file(
        artifact.locator,
        expected_byte_count=artifact.byte_count,
        maximum_byte_count=maximum_byte_count,
    )
    if hashlib.sha256(payload).hexdigest() != artifact.sha256:
        raise BatchPhenotypingError("semantic raw inventory bytes changed")
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchPhenotypingError("semantic raw inventory is not JSON") from exc
    if (
        not isinstance(raw, Mapping)
        or raw.get("schema_version") != "qupath-raw-objects/v1"
        or raw.get("slide_id") != identity["slide_id"]
        or raw.get("generation_id") != identity["tissue_generation_id"]
        or raw.get("coordinate_system") != "global_level0"
        or not isinstance(raw.get("cell_summary"), Mapping)
        or "cells" in raw
    ):
        raise BatchPhenotypingError("semantic raw hierarchy scope is invalid")
    live_binding = raw.get("semantic_live_binding")
    selected_binding = artifact.scope.analysis_domain_bindings[0]
    if (not isinstance(live_binding, Mapping)
            or set(live_binding) != (SEMANTIC_LIVE_BINDING_FIELDS | {"primary_classification_skipped"}
                if live_binding.get("primary_classification_skipped") is True else SEMANTIC_LIVE_BINDING_FIELDS)
            or raw.get("pipeline_run_id") != live_binding["run_id"]
            or type(live_binding["semantic_cell_count"]) is not int
            or live_binding["semantic_cell_count"] < 0
            or live_binding["section_id"] != identity["section_id"]
            or live_binding["generation_id"] != identity["tissue_generation_id"]
            or live_binding["canonical_image_id"] != identity["canonical_image_id"]
            or live_binding["canonical_image_sha256"] != identity["canonical_image_sha256"]
            or live_binding["segmentation_population_id"] != identity["segmentation_population_id"]
            or live_binding["semantic_cell_ids_sha256"] != identity["cell_ids_sha256"]
            or live_binding["analysis_domain_id"] != selected_binding.analysis_domain_id
            or live_binding["analysis_domain_revision_sha256"] != selected_binding.analysis_domain_revision_sha256
            or live_binding["domain_set_sha256"] != selected_binding.domain_set_sha256):
        raise BatchPhenotypingError("semantic raw accepted live binding differs from its current owner")
    live_binding = _freeze_mapping(live_binding, "semantic live binding")
    summary = raw["cell_summary"]
    ids_sha = summary.get("cell_ids_sha256")
    membership_sha = summary.get("domain_cell_mapping_sha256")
    if (ids_sha != identity["cell_ids_sha256"] or membership_sha != identity["cell_to_domain_membership_sha256"]
            or summary.get("row_count") != live_binding["semantic_cell_count"]
            or set(summary.get("domain_inventories", {})) != {live_binding["analysis_domain_id"]}):
        raise BatchPhenotypingError("semantic raw selected identity differs")
    from mif_pipeline.orchestration.analysis_services import _raw_cell_dataset
    dataset = _raw_cell_dataset(raw)
    dataset.close()
    snapshot = SemanticRawSnapshot(
        sample_id=identity["sample_id"], project_entry_id=identity["project_entry_id"],
        slide_id=identity["slide_id"], section_id=identity["section_id"],
        canonical_image_sha256=identity["canonical_image_sha256"],
        tissue_generation_id=identity["tissue_generation_id"],
        segmentation_population_id=identity["segmentation_population_id"],
        raw_export_ref_sha256=artifact.artifact_ref_sha256, raw_export_sha256=artifact.sha256,
        saved_state_after_sha256=identity["saved_state_after_sha256"],
        cell_count=summary["row_count"], analysis_domain_ids=(live_binding["analysis_domain_id"],),
        cell_ids_sha256=ids_sha, cell_to_domain_membership_sha256=membership_sha,
        primary_domain_status=identity["primary_domain_status"])
    configured = _canonical_ids(identity["configured_secondary_ids"], "configured_secondary_ids")
    candidates = _canonical_ids(
        identity["candidate_secondary_roi_ids"],
        "candidate_secondary_roi_ids",
    )
    if (
        digest(list(configured)) != identity["configured_secondary_ids_sha256"]
        or digest(list(candidates)) != identity["candidate_secondary_roi_ids_sha256"]
    ):
        raise BatchPhenotypingError("semantic secondary inventory digest differs")
    result = _projection(
        ValidatedSemanticRawInventoryProjection,
        artifact_ref=artifact,
        state_head_sha256=validated.state_head_sha256,
        active_plan_sha256=validated.active_plan_sha256,
        terminal_record_sha256=validated.terminal_record_sha256,
        phenotype_source_artifact_ref_sha256=(identity["phenotype_source_artifact_ref_sha256"]),
        configured_secondary_ids=configured,
        candidate_secondary_roi_ids=candidates,
        snapshot=snapshot,
        semantic_live_binding=live_binding,
    )
    assert isinstance(result, ValidatedSemanticRawInventoryProjection)
    return result


def decode_committed_phenotype_hierarchy_attestation(
    authority: BatchCommittedArtifactAuthority,
    *,
    maximum_byte_count: int = 16 * 1024 * 1024,
) -> ValidatedPhenotypeHierarchyAttestationProjection:
    """Reopen a current persisted hierarchy attestation from state authority."""

    validated, artifact = _committed_authority(authority)
    contract = phenotype_hierarchy_attestation_identity_contract()
    if (
        validated.producer_node_kind != "phenotype_hierarchy_attestation"
        or artifact.artifact_kind != "phenotype_view_attestation"
        or (artifact.schema_name, artifact.schema_version)
        != ("mif-batch-phenotype-view-attestation", 1)
        or artifact.identity_contract != contract
        or artifact.owning_identity_contract_sha256 != contract.contract_sha256
        or artifact.origin is not ArtifactOrigin.INTERNAL_NODE
        or artifact.locator is None
        or artifact.sha256 is None
        or artifact.byte_count is None
        or validated.execution_exchange_sha256 is not None
    ):
        raise BatchPhenotypingError("phenotype hierarchy attestation owner is invalid")
    payload = read_bounded_file(
        artifact.locator,
        expected_byte_count=artifact.byte_count,
        maximum_byte_count=maximum_byte_count,
    )
    if hashlib.sha256(payload).hexdigest() != artifact.sha256:
        raise BatchPhenotypingError("phenotype hierarchy attestation bytes changed")
    try:
        decoded = json.loads(payload.decode("utf-8"))
        if canonical_bytes(decoded) != payload:
            raise BatchPhenotypingError("attestation bytes are not canonical JSON")
        attestation = PhenotypeHierarchyAttestation.from_mapping(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("phenotype hierarchy attestation is invalid") from exc
    owning = {
        key: value for key, value in attestation.to_mapping().items() if key != "schema_version"
    }
    if (
        canonical_bytes(owning) != canonical_bytes(artifact.owning_identity)
        or artifact.scope.sample_id != attestation.sample_id
        or artifact.scope.slide_id != attestation.slide_id
        or artifact.scope.project_entry_id != attestation.project_entry_id
        or tuple(item.sample_id for item in artifact.configuration_bindings)
        != (attestation.sample_id,)
    ):
        raise BatchPhenotypingError("attestation identity differs from bytes")
    result = _projection(
        ValidatedPhenotypeHierarchyAttestationProjection,
        artifact_ref=artifact,
        state_head_sha256=validated.state_head_sha256,
        active_plan_sha256=validated.active_plan_sha256,
        terminal_record_sha256=validated.terminal_record_sha256,
        attestation=attestation,
    )
    assert isinstance(result, ValidatedPhenotypeHierarchyAttestationProjection)
    return result


def prepare_phenotype_hierarchy_attestation_directory(directory: Path) -> Path:
    """Validate and create the exact output directory before head mutation."""

    if (
        not isinstance(directory, Path)
        or not directory.is_absolute()
        or directory == Path(directory.anchor)
        or directory.is_symlink()
    ):
        raise BatchPhenotypingError("attestation directory is invalid")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir() or directory.is_symlink():
            raise BatchPhenotypingError("attestation directory is not real")
    except OSError as exc:
        raise BatchPhenotypingError("attestation directory is unavailable") from exc
    return directory


def persist_phenotype_hierarchy_attestation(
    value: PhenotypeHierarchyAttestation,
    directory: Path,
) -> tuple[Path, str, int]:
    """Publish canonical attestation bytes with atomic no-replace semantics."""

    if type(value) is not PhenotypeHierarchyAttestation:
        raise BatchPhenotypingError("attestation persistence requires exact record")
    directory = prepare_phenotype_hierarchy_attestation_directory(directory)
    payload = canonical_bytes(value.to_mapping())
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    destination = directory / f"{payload_sha256}.json"
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            dir=directory, prefix=f".{payload_sha256}.", suffix=".tmp"
        )
        temporary = Path(raw_path)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            existing = read_bounded_file(
                destination,
                expected_byte_count=len(payload),
                maximum_byte_count=len(payload),
            )
            if existing != payload:
                raise BatchPhenotypingError("content-addressed attestation contains other bytes")
        if os.name != "nt":
            directory_descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except BatchPhenotypingError:
        raise
    except OSError as exc:
        raise BatchPhenotypingError("attestation could not be persisted") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    reopened = read_bounded_file(
        destination,
        expected_byte_count=len(payload),
        maximum_byte_count=len(payload),
    )
    if reopened != payload:
        raise BatchPhenotypingError("persisted attestation bytes changed")
    return destination, payload_sha256, len(payload)


def build_cluster_label_import_invocation(
    application: ClusterLabelSectionApplication,
    prepared: PreparedPhenotypeView,
    *,
    config: AnalysisConfig,
    run_id: str,
) -> CurrentImageScientificInvocation:
    """Project exact scientific input onto the existing native importer."""

    if type(application) is not ClusterLabelSectionApplication:
        raise BatchPhenotypingError("label import invocation requires exact application")
    if type(prepared) is not PreparedPhenotypeView:
        raise BatchPhenotypingError("label import invocation requires prepared view")
    if type(config) is not AnalysisConfig:
        raise BatchPhenotypingError("label import invocation requires resolved config")
    if (
        prepared.payload.get("phenotype_generation_id") != application.phenotype_generation_id
        or prepared.payload.get("section_id") != application.section_id
        or prepared.payload.get("cell_ids_sha256") != application.cell_ids_sha256
        or prepared.payload.get("cell_to_domain_membership_sha256") != application.cell_to_domain_membership_sha256
    ):
        raise BatchPhenotypingError("prepared phenotype artifact differs from plan")
    return CurrentImageScientificInvocation(
        stage="cell_labels_import",
        config=config,
        run_id=run_id,
        project_entry_id=application.project_entry_id,
        section_id=application.section_id,
        scientific_state={
            "preflight": {
                "canonical_image_id": application.canonical_image_id,
                "canonical_image_sha256": application.canonical_image_sha256,
            },
            "tissue_acceptance": {
                "generation_id": application.tissue_generation_id,
            },
            "segmentation": {
                application.section_id: {
                    "segmentation_population_id": (application.segmentation_population_id)
                }
            },
            "cluster_label_application": {
                "selected_display_view": "unsupervised_clustering",
                "materialize_display_classes": True,
                "phenotype_view_operation": "store_and_materialize",
                "phenotype_view_artifact": prepared.to_request_mapping(),
                "phenotype_assignment_source": dict(prepared.assignment_source),
            },
        },
    )


def _closed_mapping(value: object, fields: frozenset[str], field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise BatchArtifactError(f"{field} is not the current closed mapping")
    return value


def build_cluster_label_import_execution_result(
    evidence: ProjectStageTargetExecutionEvidence,
    application: ClusterLabelSectionApplication,
    prepared: PreparedPhenotypeView,
) -> BatchNodeExecutionResult:
    """Validate native output and publish only its compact identity."""

    if type(evidence) is not ProjectStageTargetExecutionEvidence:
        raise BatchArtifactError("cluster-label import requires Task-10 evidence")
    if type(application) is not ClusterLabelSectionApplication:
        raise BatchArtifactError("cluster-label import requires exact application")
    if type(prepared) is not PreparedPhenotypeView:
        raise BatchArtifactError("cluster-label import requires prepared view")
    context = evidence.context
    authorized = evidence.authorized
    observation = evidence.observation
    validation_inputs = evidence._validation_inputs._reopen(context=context, authorized=authorized)
    if (
        context.adapter_spec.node_kind != "clustering_label_import"
        or authorized.terminal_stage_id != "clustering_label_import"
        or authorized.wire_stage_id != "cell_labels_import"
        or authorized.request_codec_id != QuPathScriptAsset.IMPORT_PYTHON_CELL_LABELS.value
        or authorized.request_codec_version != "1"
        or authorized.sample_id != application.sample_id
        or authorized.project_entry_id != application.project_entry_id
        or authorized.slide_id != application.slide_id
        or authorized.saved_state_before_sha256 != application.expected_saved_state_before_sha256
        or tuple(sorted(authorized.prerequisite_artifact_ref_sha256s))
        != application.source_ref_sha256s
        or tuple(sorted(item.artifact_ref_sha256 for item in context.inputs))
        != application.source_ref_sha256s
    ):
        raise BatchArtifactError("cluster-label Task-10 target differs from application")
    result = evidence.wire_stage_result
    if result.status is not StageStatus.COMPLETED:
        return BatchNodeExecutionResult(context.authorization.authorization_sha256, ())
    output_fields = frozenset(
        {
            "component_descriptor",
            "cell_to_domain_membership_sha256",
            "cell_ids_sha256",
            "cell_count",
            "section_id",
            "selected_display_view",
            "phenotype_generation_id",
            "phenotype_view_artifact",
            "phenotype_view_operation",
            "materialized_display_classes",
            "hierarchy_artifact_sha256",
            "coordinate_system",
        }
    )
    outputs = _closed_mapping(result.outputs, output_fields, "cell-label output")
    manifest = _closed_mapping(
        evidence.generation_manifest,
        frozenset(
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
        ),
        "cell-label generation manifest",
    )
    expected_ids = tuple(item["cell_id"] for item in application.import_rows)
    if (
        result.stage != "cell_labels_import"
        or result.slide_id != application.slide_id
        or result.generation_id != application.tissue_generation_id
        or outputs["cell_ids_sha256"] != application.cell_ids_sha256
        or outputs["cell_count"] != len(expected_ids)
        or outputs["cell_to_domain_membership_sha256"] != application.cell_to_domain_membership_sha256
        or outputs["section_id"] != application.section_id
        or outputs["selected_display_view"] != "unsupervised_clustering"
        or outputs["phenotype_generation_id"] != application.phenotype_generation_id
        or outputs["phenotype_view_artifact"] != prepared.to_request_mapping()
        or isinstance(outputs["phenotype_view_artifact"].get("cell_count"), bool)
        or outputs["phenotype_view_operation"] != "store_and_materialize"
        or outputs["materialized_display_classes"] is not True
        or outputs["hierarchy_artifact_sha256"] != prepared.artifact_sha256
        or outputs["coordinate_system"] != "global_level0"
        or manifest["schema_version"] != "roi-generation-manifest/v1"
        or manifest["invocation_id"] != authorized.attempt_id
        or manifest["run_id"] != authorized.pipeline_run_id
        or manifest["stage"] != "cell_labels_import"
        or manifest["slide_id"] != application.slide_id
        or tuple(manifest["generation_ids"]) != (application.tissue_generation_id,)
        or tuple(manifest["generations"]) != ()
        or canonical_bytes(manifest["object_outputs"]) != canonical_bytes(result.outputs)
        or observation.saved_state_after_sha256 is None
        or observation.saved_state_disposition == "unattestable"
    ):
        raise BatchArtifactError("native cluster-label result is incomplete or stale")
    identity: dict[str, object] = {
        "component_descriptor": dict(freeze_component_reference(outputs["component_descriptor"])),
        "sample_id": application.sample_id,
        "slide_id": application.slide_id,
        "project_entry_id": application.project_entry_id,
        "canonical_image_id": application.canonical_image_id,
        "canonical_image_sha256": application.canonical_image_sha256,
        "section_id": application.section_id,
        "tissue_generation_id": application.tissue_generation_id,
        "tissue_path_object_id": application.tissue_path_object_id,
        "tissue_roi_id": application.tissue_roi_id,
        "segmentation_generation_id": application.segmentation_generation_id,
        "segmentation_population_id": application.segmentation_population_id,
        "measurement_generation_id": application.measurement_generation_id,
        "clustering_generation_sha256": application.clustering_generation_sha256,
        "accepted_review_sha256": application.accepted_review_sha256,
        "composite_projection_sha256": application.composite_projection_sha256,
        "cell_ids_sha256": application.cell_ids_sha256,
        "cell_to_domain_membership_sha256": (application.cell_to_domain_membership_sha256),
        "phenotype_generation_id": application.phenotype_generation_id,
        "phenotype_view_artifact_sha256": prepared.artifact_sha256,
        "phenotype_view_lineage_sha256": prepared.lineage_sha256,
        "source_ref_sha256s": list(application.source_ref_sha256s),
        "saved_state_before_sha256": authorized.saved_state_before_sha256,
        "saved_state_after_sha256": observation.saved_state_after_sha256,
        "saved_state_disposition": observation.saved_state_disposition,
        "execution_exchange_sha256": evidence.execution_exchange_sha256,
        "validated_exchange_sha256": evidence.validated_exchange_sha256,
        "generation_manifest_payload_sha256": (observation.generation_manifest_payload_sha256),
        "result_payload_sha256": observation.result_payload_sha256,
        "node_id": authorized.node_id,
        "attempt_id": authorized.attempt_id,
        "attempt_authorization_sha256": authorized.attempt_authorization_sha256,
        "input_artifact_set_sha256": authorized.input_artifact_set_sha256,
        "adapter_spec_sha256": context.adapter_spec.spec_sha256,
        "projection_target_sha256": context.projection_target_sha256,
        "compatibility_signature_sha256": context.compatibility_signature_sha256,
        "request_codec_id": authorized.request_codec_id,
        "request_codec_version": authorized.request_codec_version,
    }
    identity["task10_execution_identity_sha256"] = digest(identity)
    contract = cluster_label_import_result_identity_contract()
    measurement_refs = tuple(
        item for item in context.inputs if item.artifact_kind == "measurements_result"
    )
    if len(measurement_refs) != 1:
        raise BatchArtifactError("cluster-label import requires one measurement owner")
    measurement_ref = measurement_refs[0]
    artifact = BatchArtifactRef(
        artifact_id="cluster-label-import-" + digest(identity)[:48],
        artifact_kind=CLUSTER_LABEL_IMPORT_RESULT_KIND,
        schema_name=CLUSTER_LABEL_IMPORT_RESULT_SCHEMA_NAME,
        schema_version=CLUSTER_LABEL_IMPORT_RESULT_SCHEMA_VERSION,
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
            measurement_ref.scope.validation_domain_id,
            application.sample_id,
            application.slide_id,
            application.project_entry_id,
            measurement_ref.scope.analysis_domain_bindings,
        ),
        owning_identity=identity,
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version=_PRODUCER_VERSION,
        runtime_versions={
            "mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION,
            "qupath": measurement_ref.runtime_versions["qupath"],
        },
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=measurement_ref.configuration_bindings,
    )
    return BatchNodeExecutionResult(authorized.attempt_authorization_sha256, (artifact,))


__all__ = [
    "CLUSTER_LABEL_IMPORT_RESULT_KIND",
    "build_cluster_label_import_execution_result",
    "build_cluster_label_import_invocation",
    "cluster_label_import_result_identity_contract",
    "decode_committed_accepted_cluster_label_review",
    "decode_committed_cluster_label_import_result",
    "decode_committed_clustering_result_for_application",
    "decode_committed_phenotype_hierarchy_attestation",
    "decode_committed_semantic_raw_inventory",
    "persist_phenotype_hierarchy_attestation",
    "prepare_phenotype_hierarchy_attestation_directory",
]
