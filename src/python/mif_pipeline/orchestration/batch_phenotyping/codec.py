"""Strict canonical codecs and idempotent persistence for clustering results.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import fields
from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import MappingProxyType

from mif_pipeline.cell_analysis.clustering import ClusteringEvidence
from mif_pipeline.pipeline_core.config import ClusteringMode
from mif_pipeline.orchestration.batch_artifacts import (
    ArtifactOrigin,
    BatchArtifactError,
    BatchCommittedArtifactAuthority,
    validate_artifact_ref,
    validate_committed_artifact_authority_set,
)

from ._common import (
    RESULT_SCHEMA,
    BatchPhenotypingError,
    canonical_bytes,
    closed_mapping,
    digest,
    exact_tuple,
    mapping,
    optional_text,
    read_bounded_file,
    sequence,
    sha256,
    text,
)
from .records import (
    BatchClusterAssignment,
    BatchClusterRefinement,
    BatchClusteringEntryComposition,
    BatchClusteringExecutionResult,
    BatchClusteringLabelProjection,
    PersistedBatchClusteringResult,
    PersistedUnsupervisedPreparation,
    UnsupervisedPreparationInput,
)

# ==============================================================================
#                                     Codec
# ==============================================================================


def _evidence_from_mapping(value: object) -> ClusteringEvidence:
    names = frozenset(item.name for item in fields(ClusteringEvidence))
    optional = frozenset({'method_settings', 'model_codebook', 'model_clusters', 'model_sha256', 'model_group_labels'})
    supplied = frozenset(value) if isinstance(value, Mapping) else frozenset()
    value = closed_mapping(value, (names - optional) | (supplied & optional), "clustering evidence")
    tuple_names = {
        "feature_measurements",
        "canonical_biomarkers",
        "robust_centers",
        "robust_scales",
        "feature_weights",
        "model_clusters",
    }
    kwargs = {
        name: tuple(tuple(row) for row in raw) if name in {'method_settings', 'model_codebook', 'model_group_labels'} else tuple(sequence(raw, name)) if name in tuple_names else raw
        for name, raw in value.items()
    }
    try:
        evidence = ClusteringEvidence(**kwargs)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise BatchPhenotypingError("clustering evidence is invalid") from exc
    if (
        evidence.graph_algorithm not in {"exact_snn_jaccard_leiden/v1", "phenograph_leiden/v1", "flowsom/v1"}
        or type(evidence.node_count) is not int
        or evidence.node_count < 0
        or type(evidence.edge_count) is not int
        or evidence.edge_count < 0
        or type(evidence.component_count) is not int
        or evidence.component_count < 0
        or type(evidence.isolate_count) is not int
        or evidence.isolate_count < 0
    ):
        raise BatchPhenotypingError("clustering evidence is invalid")
    for name in ("feature_transform_sha256", "node_identity_sha256", *(('model_sha256',) if evidence.graph_algorithm == 'flowsom/v1' else ('graph_sha256',))):
        sha256(getattr(evidence, name), name)
    return evidence


def dumps_batch_clustering_result(value: BatchClusteringExecutionResult) -> bytes:
    if type(value) is not BatchClusteringExecutionResult:
        raise BatchPhenotypingError("clustering result must be an exact owner record")
    return canonical_bytes(value.to_mapping())


def dumps_unsupervised_preparation(value: UnsupervisedPreparationInput) -> bytes:
    if type(value) is not UnsupervisedPreparationInput:
        raise BatchPhenotypingError("preparation must be an exact owner record")
    return canonical_bytes(value.to_mapping())


def persist_unsupervised_preparation(
    value: UnsupervisedPreparationInput,
    directory: Path,
) -> PersistedUnsupervisedPreparation:
    """Publish one immutable content-addressed preparation descriptor."""

    if type(value) is not UnsupervisedPreparationInput or not isinstance(directory, Path):
        raise BatchPhenotypingError("preparation persistence inputs are invalid")
    if not directory.is_absolute() or directory == Path(directory.anchor):
        raise BatchPhenotypingError("preparation directory must be absolute and non-root")
    if directory.is_symlink() or not directory.is_dir():
        raise BatchPhenotypingError("preparation directory must be a real directory")
    payload = dumps_unsupervised_preparation(value)
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    destination = directory / f"{payload_sha256}.json"

    temporary: Path | None = None
    try:
        descriptor, raw_temporary = tempfile.mkstemp(
            dir=directory,
            prefix=f".{payload_sha256}.",
            suffix=".tmp",
        )
        temporary = Path(raw_temporary)
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
                raise BatchPhenotypingError("content-addressed preparation contains other bytes")
        if os.name != "nt":
            directory_descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except BatchPhenotypingError:
        raise
    except OSError as exc:
        raise BatchPhenotypingError("preparation could not be persisted") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    reopened = read_bounded_file(
        destination,
        expected_byte_count=len(payload),
        maximum_byte_count=len(payload),
    )
    if reopened != payload:
        raise BatchPhenotypingError("persisted preparation bytes changed")
    return PersistedUnsupervisedPreparation(
        locator=destination, sha256=payload_sha256, byte_count=len(payload)
    )


def _loads_batch_clustering_result(
    payload: bytes,
    *,
    expected_request_sha256: str,
    expected_source_authority_sha256s: tuple[str, ...],
) -> BatchClusteringExecutionResult:
    """Reopen a result only for its exact request and authenticated source set."""

    if type(payload) is not bytes or not payload:
        raise BatchPhenotypingError("clustering result payload must be nonempty bytes")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchPhenotypingError("clustering result is not JSON") from exc
    if canonical_bytes(decoded) != payload:
        raise BatchPhenotypingError("clustering result JSON is not canonical")
    names = frozenset(
        {
            "schema_version",
            "mode",
            "phenotyping_group_id",
            "participant_ids",
            "project_entry_ids",
            "request_sha256",
            "source_authority_sha256s",
            "source_state_head_sha256",
            "active_plan_sha256",
            "entry_composition",
            "assignments",
            "entry_projections",
            "evidence",
            "runtime_versions",
            "label_review_status",
            "input_population_sha256",
            "output_assignment_sha256",
            "clustering_generation_sha256",
            "result_sha256",
        }
    )
    if isinstance(decoded,Mapping) and 'refinement' in decoded:
        names=names|{'refinement'}
    value = closed_mapping(decoded, names, "clustering result")
    if value["schema_version"] != RESULT_SCHEMA:
        raise BatchPhenotypingError("clustering result schema is unsupported")
    material = {name: value[name] for name in names if name != "result_sha256"}
    if digest(material) != sha256(value["result_sha256"], "result_sha256"):
        raise BatchPhenotypingError("clustering result digest is invalid")
    request_sha = sha256(value["request_sha256"], "request_sha256")
    sources = tuple(
        sha256(item, "source_authority_sha256s")
        for item in sequence(value["source_authority_sha256s"], "source_authority_sha256s")
    )
    expected_sources = tuple(
        sorted(
            sha256(item, "expected_source_authority_sha256s")
            for item in exact_tuple(
                expected_source_authority_sha256s, "expected_source_authority_sha256s"
            )
        )
    )
    if (
        request_sha != sha256(expected_request_sha256, "expected_request_sha256")
        or sources != expected_sources
    ):
        raise BatchPhenotypingError("clustering result belongs to another request or population")
    runtime = mapping(value["runtime_versions"], "runtime_versions")
    if not runtime or any(
        type(key) is not str or type(raw) is not str or not raw for key, raw in runtime.items()
    ):
        raise BatchPhenotypingError("clustering runtime versions are invalid")
    try:
        result = BatchClusteringExecutionResult(
            refinement=(BatchClusterRefinement.from_mapping(value['refinement']) if 'refinement' in value else None),
            mode=ClusteringMode(value["mode"]),
            phenotyping_group_id=optional_text(
                value["phenotyping_group_id"], "phenotyping_group_id"
            ),
            participant_ids=tuple(
                text(x, "participant_ids")
                for x in sequence(value["participant_ids"], "participant_ids")
            ),
            project_entry_ids=tuple(
                text(x, "project_entry_ids")
                for x in sequence(value["project_entry_ids"], "project_entry_ids")
            ),
            request_sha256=request_sha,
            source_authority_sha256s=sources,
            source_state_head_sha256=sha256(
                value["source_state_head_sha256"], "source_state_head_sha256"
            ),
            active_plan_sha256=sha256(value["active_plan_sha256"], "active_plan_sha256"),
            entry_composition=tuple(
                BatchClusteringEntryComposition.from_mapping(x)
                for x in sequence(value["entry_composition"], "entry_composition")
            ),
            assignments=tuple(
                BatchClusterAssignment.from_mapping(x)
                for x in sequence(value["assignments"], "assignments")
            ),
            entry_projections=tuple(
                BatchClusteringLabelProjection.from_mapping(x)
                for x in sequence(value["entry_projections"], "entry_projections")
            ),
            label_review_status=text(value["label_review_status"], "label_review_status"),
            input_population_sha256=sha256(
                value["input_population_sha256"], "input_population_sha256"
            ),
            output_assignment_sha256=sha256(
                value["output_assignment_sha256"], "output_assignment_sha256"
            ),
            evidence=_evidence_from_mapping(value["evidence"]),
            runtime_versions=MappingProxyType(dict(sorted(runtime.items()))),  # type: ignore[arg-type]
            clustering_generation_sha256=sha256(
                value["clustering_generation_sha256"], "clustering_generation_sha256"
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("clustering result content is invalid") from exc
    if result.result_sha256 != value["result_sha256"]:
        raise BatchPhenotypingError("clustering result relationships are invalid")
    return result


def decode_committed_batch_clustering_result(
    authority: BatchCommittedArtifactAuthority,
    *,
    source_authorities: tuple[BatchCommittedArtifactAuthority, ...],
    maximum_byte_count: int,
) -> BatchClusteringExecutionResult:
    """Reopen a bounded result only through current result and source authorities."""

    if (
        type(authority) is not BatchCommittedArtifactAuthority
        or type(source_authorities) is not tuple
    ):
        raise BatchPhenotypingError("clustering result decoding requires committed authorities")
    try:
        validate_committed_artifact_authority_set(
            (authority, *source_authorities),
            expected_active_plan_sha256=authority.active_plan_sha256,
        )
        sources = tuple(
            sorted(
                source_authorities,
                key=lambda item: item.artifact_ref.artifact_ref_sha256,
            )
        )
        ref = validate_artifact_ref(authority.artifact_ref)
    except (BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("clustering result authority is invalid") from exc
    from .authority import (
        clustering_result_identity_contract,
        clustering_result_owning_identity,
    )

    if (
        authority.producer_node_kind
        not in {"clustering_per_slide", "clustering_pooled"}
        or ref.artifact_kind != "clustered_phenotype_result"
        or (ref.schema_name, ref.schema_version) != ("mif-batch-clustering-result", 1)
        or ref.identity_contract != clustering_result_identity_contract()
        or ref.origin is not ArtifactOrigin.INTERNAL_NODE
        or ref.locator is None
        or ref.sha256 is None
        or ref.byte_count is None
    ):
        raise BatchPhenotypingError("clustering result ref lacks its owner contract")
    source_refs = tuple(sorted(item.artifact_ref.artifact_ref_sha256 for item in sources))
    if tuple(ref.owning_identity.get("source_authority_sha256s", ())) != source_refs:
        raise BatchPhenotypingError("clustering result source authority set differs")
    expected_bindings = tuple(
        sorted(
            {
                binding
                for item in sources
                if item.producer_node_kind == "unsupervised_preparation"
                for binding in item.artifact_ref.scope.analysis_domain_bindings
            },
            key=lambda item: (
                item.sample_id,
                item.slide_id,
                item.analysis_domain_id,
                item.analysis_domain_revision,
                item.project_entry_id,
            ),
        )
    )
    if ref.scope.analysis_domain_bindings != expected_bindings:
        raise BatchPhenotypingError("clustering result domain scope differs from sources")
    payload = read_bounded_file(
        ref.locator,
        expected_byte_count=ref.byte_count,
        maximum_byte_count=maximum_byte_count,
    )
    if hashlib.sha256(payload).hexdigest() != ref.sha256:
        raise BatchPhenotypingError("clustering result bytes changed")
    result = _loads_batch_clustering_result(
        payload,
        expected_request_sha256=ref.owning_identity.get("request_sha256"),
        expected_source_authority_sha256s=source_refs,
    )
    if canonical_bytes(clustering_result_owning_identity(result)) != canonical_bytes(
        ref.owning_identity
    ):
        raise BatchPhenotypingError("clustering result identity differs from its bytes")
    expected_kind = {
        ClusteringMode.PER_SLIDE: "clustering_per_slide",
        ClusteringMode.POOLED: "clustering_pooled",
    }[result.mode]
    if authority.producer_node_kind != expected_kind:
        raise BatchPhenotypingError("clustering result producer scope differs")
    preparation_refs = tuple(
        item.artifact_ref
        for item in sources
        if item.producer_node_kind == "unsupervised_preparation"
    )
    if result.mode is ClusteringMode.POOLED:
        domains = {item.scope.validation_domain_id for item in preparation_refs}
        if len(domains) != 1 or None in domains:
            raise BatchPhenotypingError("pooled result requires one Processing group")
        expected_scope = type(ref.scope)(next(iter(domains)), None, None, None, expected_bindings)
    else:
        validation_domains = {item.scope.validation_domain_id for item in preparation_refs}
        sample_ids = {item.scope.sample_id for item in preparation_refs}
        slide_ids = {item.scope.slide_id for item in preparation_refs}
        entry_ids = {item.scope.project_entry_id for item in preparation_refs}
        if any(
            len(values) != 1 or None in values
            for values in (validation_domains, sample_ids, slide_ids, entry_ids)
        ):
            raise BatchPhenotypingError(
                "per_slide clustering sources lack one exact artifact scope"
            )
        expected_scope = type(ref.scope)(
            next(iter(validation_domains)),
            next(iter(sample_ids)),
            next(iter(slide_ids)),
            next(iter(entry_ids)),
            expected_bindings,
        )
    if ref.scope != expected_scope:
        raise BatchPhenotypingError("clustering result top-level scope differs")
    expected_configurations = tuple(
        sorted(
            {binding for item in sources for binding in item.artifact_ref.configuration_bindings},
            key=lambda item: item.sample_id,
        )
    )
    if ref.configuration_bindings != expected_configurations:
        raise BatchPhenotypingError("clustering result configuration scope differs from sources")
    return result


def persist_batch_clustering_result(
    result: BatchClusteringExecutionResult,
    destination: Path,
) -> PersistedBatchClusteringResult:
    """Publish bytes exclusively; permit only same-result idempotent reopening."""

    if type(result) is not BatchClusteringExecutionResult or not isinstance(destination, Path):
        raise BatchPhenotypingError("clustering persistence inputs are invalid")
    if not destination.is_absolute() or destination == Path(destination.anchor):
        raise BatchPhenotypingError("clustering result locator must be absolute and non-root")
    payload = dumps_batch_clustering_result(result)
    if destination.is_symlink():
        raise BatchPhenotypingError("clustering result locator must not be a symlink")
    if destination.exists():
        try:
            existing = read_bounded_file(
                destination,
                expected_byte_count=len(payload),
                maximum_byte_count=len(payload),
            )
        except BatchPhenotypingError as exc:
            raise BatchPhenotypingError("existing clustering result is unreadable") from exc
        if existing != payload:
            raise BatchPhenotypingError("clustering result destination contains other bytes")
    else:
        temporary: Path | None = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, raw_temporary = tempfile.mkstemp(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
            )
            temporary = Path(raw_temporary)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, destination)
            temporary.unlink()
            temporary = None
            if os.name != "nt":
                directory = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except FileExistsError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            return persist_batch_clustering_result(result, destination)
        except OSError as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise BatchPhenotypingError(
                f"Clustering results could not be saved to {destination.parent}. "
                "Check that the analysis folder is writable and has free space, then retry this calculation."
            ) from exc
    return PersistedBatchClusteringResult(
        locator=destination,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_count=len(payload),
        result_sha256=result.result_sha256,
    )


__all__ = [
    "decode_committed_batch_clustering_result",
    "dumps_batch_clustering_result",
    "dumps_unsupervised_preparation",
    "persist_batch_clustering_result",
    "persist_unsupervised_preparation",
]
