"""Mode-neutral accepted-analysis-domain artifact composition.

The `ValidatedAcceptedAnalysisDomainLiveAuthority` record carries `committed`,
`accepted_tissue`, `manifests`, `overlap_evidence`, `accepted_tissue_request_rows` and its
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
import json
import re
from typing import Final

from mif_pipeline.analysis_domain import (
    ActiveAnalysisDomainAuthority,
    AnalysisDomainError,
    AnalysisDomainManifest,
    DomainOverlapEvidence,
    resolve_active_analysis_domain_authorities,
    validate_domain_manifest,
)
from mif_pipeline.cell_analysis import DomainSetEvidence
from mif_pipeline.pipeline_core import (
    AnalysisConfig, AnalysisDomainKind, InteractionReason, StageResult, StageStatus,
)
from mif_pipeline.qupath_runtime.scripts import QuPathScriptAsset

from .batch_analysis_preflight import (
    MIF_PIPELINE_BATCH_RUNTIME_VERSION,
    BatchAnalysisPreflightError,
    BatchAnalysisPreflightEvidence,
    validate_analysis_frame_artifact_ref,
)
from .batch_artifacts import (
    ArtifactConfigurationBinding,
    ArtifactMultiplicity,
    ArtifactOrigin,
    ArtifactScope,
    BatchArtifactError,
    BatchArtifactRef,
    BatchCommittedArtifactAuthority,
    BatchCurrentExecutionAuthority,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    validate_current_execution_authority,
)
from .batch_tissue_artifacts import (
    ValidatedAcceptedTissueLiveAuthority,
    _validate_accepted_tissue_live_authority,
    validate_tissue_acceptance_result_artifact_ref,
)
from .accepted_analysis_domain_records import (
    ACCEPTED_ANALYSIS_DOMAIN_IDENTITY_FIELDS,
    ACCEPTED_ANALYSIS_DOMAIN_OPTIONAL_IDENTITY_FIELDS,
    MAX_ACCEPTED_ANALYSIS_DOMAIN_MEMBERS,
    SELECTED_MODE as _SELECTED_MODE,
    RETIRED_WHOLE_MODE,
    ValidatedAcceptedAnalysisDomainArtifactRefProjection,
    ValidatedAcceptedAnalysisDomainProjection,
    accepted_analysis_domain_identity_contract,
    analysis_domain_bindings as _bindings,
    decode_committed_accepted_analysis_domain,
    domain_set_from_authorities as _domain_set,
    validate_accepted_analysis_domain_artifact_ref,
)
from .project_stage_codecs import ProjectStageCodecValidationResult
from .project_stage_plan import (
    ProjectStageCommittedExchangeReopener,
    ProjectStageRecordError,
    ProjectStageTargetExecutionEvidence,
)

_ARTIFACT_KIND: Final[str] = "accepted_analysis_domain"
_SCHEMA_NAME: Final[str] = "mif-batch-accepted-analysis-domain"
_SCHEMA_VERSION: Final[int] = 1
_PRODUCER_VERSION: Final[str] = "mif-pipeline-batch-accepted-analysis-domain/v1"
_GENERATION_MANIFEST_SCHEMA: Final[str] = "roi-generation-manifest/v1"
_SELECTED_OBJECT_SCHEMA: Final[str] = "analysis-domain-candidate-manifest/v1"
_NODE_KIND: Final[str] = "analysis_domain_selection"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ACCEPTED_ANALYSIS_DOMAIN_LIVE_ISSUER = object()

_GENERATION_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "invocation_id",
        "run_id",
        "stage",
        "slide_id",
        "generation_ids",
        "object_outputs",
    }
)
_SELECTED_OBJECT_FIELDS = frozenset(
    {
        "schema_version",
        "selection_status",
        "canonical_image_id",
        "canonical_image_sha256",
        "project_entry_id",
        "tissue_generation_id",
        "analysis_domain_manifests",
        "overlap_evidence",
        "domain_set_sha256",
        "saved_closed_acknowledged",
        "selected_annotations_locked",
        "assistance",
    }
)


# ==============================================================================
#                        Batch analysis domain artifacts
# ==============================================================================


class UnsupportedBatchAnalysisDomainProducerError(BatchArtifactError):
    """Raised when a producer lacks an authenticated prerequisite decoder."""


@dataclass(frozen=True, slots=True, init=False)
class ValidatedAcceptedAnalysisDomainLiveAuthority:
    """Complete selected-domain histories reauthenticated from Task-6 bytes."""

    committed: ValidatedAcceptedAnalysisDomainProjection
    accepted_tissue: ValidatedAcceptedTissueLiveAuthority
    manifests: tuple[AnalysisDomainManifest, ...]
    overlap_evidence: tuple[DomainOverlapEvidence, ...]
    accepted_tissue_request_rows: tuple[Mapping[str, object], ...]
    domain_authorities: tuple[ActiveAnalysisDomainAuthority, ...]
    domain_set: DomainSetEvidence
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("ValidatedAcceptedAnalysisDomainLiveAuthority is issuer-only")

    def __reduce__(self) -> object:
        raise TypeError("ValidatedAcceptedAnalysisDomainLiveAuthority is nonserializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("ValidatedAcceptedAnalysisDomainLiveAuthority is nonserializable")

    def __copy__(self) -> object:
        raise TypeError("ValidatedAcceptedAnalysisDomainLiveAuthority is noncopyable")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("ValidatedAcceptedAnalysisDomainLiveAuthority is noncopyable")


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
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
    except (TypeError, ValueError, OverflowError) as exc:
        raise BatchArtifactError("accepted analysis-domain evidence is not finite JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BatchArtifactError(f"{field} must be nonblank canonical text")
    return value


def _sha256(value: object, field: str) -> str:
    digest = _text(value, field)
    if _SHA256.fullmatch(digest) is None:
        raise BatchArtifactError(f"{field} must be a lowercase SHA-256")
    return digest


def _positive_integer(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise BatchArtifactError(f"{field} must be a positive integer")
    return value


def _closed_mapping(value: object, fields: frozenset[str], field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise BatchArtifactError(f"{field} must be a string-keyed object")
    unknown = sorted(set(value) - fields)
    missing = sorted(fields - set(value))
    if unknown:
        raise BatchArtifactError(f"{field} has unknown field(s): {', '.join(unknown)}")
    if missing:
        raise BatchArtifactError(f"{field} lacks required field(s): {', '.join(missing)}")
    return value


def _sequence(value: object, field: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise BatchArtifactError(f"{field} must be an array")
    return tuple(value)


def _source_ref_sha256s(inputs: Sequence[BatchArtifactRef]) -> tuple[str, ...]:
    values = tuple(sorted(item.artifact_ref_sha256 for item in inputs))
    if not values or len(set(values)) != len(values):
        raise BatchArtifactError(
            "accepted analysis-domain source-input references must be nonempty and unique"
        )
    return values


def _saved_state(disposition: object, saved_state_after_sha256: object) -> tuple[str, str]:
    value = _text(disposition, "saved_state_disposition")
    if value not in {"authenticated_unchanged", "authenticated_changed"}:
        raise BatchArtifactError("accepted analysis-domain saved-state disposition is invalid")
    return value, _sha256(saved_state_after_sha256, "saved_state_after_sha256")


def _artifact(
    context: object,
    *,
    selection_mode: str,
    sample_id: str,
    slide_id: str,
    validation_domain_id: str,
    project_entry_id: str,
    canonical_image_id: str,
    canonical_image_sha256: str,
    tissue_generation_id: str,
    source_input_artifact_ref_sha256s: tuple[str, ...],
    accepted_output_sha256: str,
    resolved_config_sha256: str,
    saved_state_disposition: str,
    saved_state_after_sha256: str,
    assistance_sha256: str,
    domain_set: DomainSetEvidence,
    domain_authorities: tuple[ActiveAnalysisDomainAuthority, ...],
    mode_evidence: Mapping[str, object],
    runtime_versions: Mapping[str, str],
) -> BatchArtifactRef:
    authorization = context.authorization
    identity = {
        "selection_mode": selection_mode,
        "sample_id": sample_id,
        "slide_id": slide_id,
        "project_entry_id": project_entry_id,
        "canonical_image_id": canonical_image_id,
        "canonical_image_sha256": canonical_image_sha256,
        "tissue_generation_id": tissue_generation_id,
        "source_input_artifact_ref_sha256s": list(source_input_artifact_ref_sha256s),
        "accepted_output_sha256": accepted_output_sha256,
        "resolved_config_sha256": resolved_config_sha256,
        "assistance_sha256": assistance_sha256,
        "saved_state_disposition": saved_state_disposition,
        "saved_state_after_sha256": saved_state_after_sha256,
        "domain_set": domain_set.to_mapping(),
        "domain_authorities": [item.to_mapping() for item in domain_authorities],
        "mode_evidence": dict(mode_evidence),
    }
    contract = accepted_analysis_domain_identity_contract()
    return BatchArtifactRef(
        artifact_id="accepted-domains-" + _digest(identity)[:48],
        artifact_kind=_ARTIFACT_KIND,
        schema_name=_SCHEMA_NAME,
        schema_version=_SCHEMA_VERSION,
        origin=ArtifactOrigin.INTERNAL_NODE,
        producer_node_id=context.node_id,
        producer_attempt_id=authorization.attempt_id,
        producer_attempt_authorization_sha256=authorization.authorization_sha256,
        external_declaration_sha256=None,
        external_validation_receipt_sha256=None,
        locator=None,
        sha256=None,
        byte_count=None,
        scope=ArtifactScope(
            validation_domain_id=validation_domain_id,
            sample_id=sample_id,
            slide_id=slide_id,
            project_entry_id=project_entry_id,
            analysis_domain_bindings=_bindings(
                sample_id=sample_id,
                authorities=domain_authorities,
                domain_set_sha256=domain_set.domain_set_sha256,
            ),
        ),
        owning_identity=identity,
        identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version=_PRODUCER_VERSION,
        runtime_versions=runtime_versions,
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=(
            ArtifactConfigurationBinding(
                sample_id,
                resolved_config_sha256,
            ),
        ),
    )


def _validated_evidence_sources(
    evidence: ProjectStageTargetExecutionEvidence,
) -> tuple[object, object, object]:
    if type(evidence) is not ProjectStageTargetExecutionEvidence:
        raise BatchArtifactError(
            "accepted analysis-domain composition requires Repair6B execution evidence"
        )
    context = evidence.context
    authorized = evidence.authorized
    observation = evidence.observation
    if (
        context.authorization.attempt_id != authorized.attempt_id
        or context.authorization.authorization_sha256 != authorized.attempt_authorization_sha256
        or context.node_id != authorized.node_id
        or observation.node_id != authorized.node_id
        or observation.attempt_id != authorized.attempt_id
        or observation.attempt_authorization_sha256 != authorized.attempt_authorization_sha256
        or observation.authorized_entry_sha256 != authorized.authorized_entry_sha256
        or observation.project_entry_id != authorized.project_entry_id
        or observation.sample_id != authorized.sample_id
        or observation.slide_id != authorized.slide_id
        or evidence.execution_exchange_sha256 != authorized.exchange.exchange_sha256
    ):
        raise BatchArtifactError("Repair6B execution evidence sources disagree")
    _sha256(evidence.validated_exchange_sha256, "validated_exchange_sha256")
    _sha256(evidence.execution_exchange_sha256, "execution_exchange_sha256")
    return context, authorized, observation


def _selected_region_review(
    evidence: ProjectStageTargetExecutionEvidence,
    output: Mapping[str, object],
    frame: BatchAnalysisPreflightEvidence,
) -> ProjectStageCodecValidationResult:
    """Keep the saved selection available for review before accepting its regions.

    We validate geometry against the original request and accepted tissue. The
    review retains a compact reference to that exact snapshot; accepting it later
    locks the selected annotations and publishes the accepted analysis regions.
    """
    from .workflow import (
        CurrentImageScientificInvocation,
        _analysis_domain_manifest_reference,
        _candidate_path_object_ids,
        _validated_analysis_domain_context_manifest,
    )

    inputs = evidence.reopen_validation_inputs()
    request = inputs.scientific_request_body
    authorized = evidence.authorized
    if (
        request.get("action") != "review_analysis_rois"
        or request.get("saved_closed_acknowledged") is not True
        or request.get("generation_id") != output["tissue_generation_id"]
        or _plain(request.get("analysis_domain_assistance")) != _plain(output["assistance"])
    ):
        raise BatchArtifactError("candidate regions differ from the requested saved review")
    invocation = CurrentImageScientificInvocation(
        stage=_NODE_KIND,
        config=AnalysisConfig.from_mapping(_plain(inputs.resolved_config)),
        run_id=authorized.pipeline_run_id,
        project_entry_id=authorized.project_entry_id,
        action="review_analysis_rois",
        scientific_state={
            "preflight": {
                "canonical_image_id": frame.spatial_frame.canonical_image_id,
                "canonical_image_sha256": frame.spatial_frame.canonical_image_sha256,
            },
            "tissue_acceptance": {
                "generation_id": request["generation_id"],
                "sections": [
                    {"section_id": row["section_id"], "roi_id": row["tissue_roi_id"],
                     "path_object_id": row["tissue_path_object_id"]}
                    for row in request["accepted_tissue_sections"]
                ],
            },
        },
    )
    members = _sequence(output["analysis_domain_manifests"], "analysis_domain_manifests")
    if not members or len(members) > MAX_ACCEPTED_ANALYSIS_DOMAIN_MEMBERS:
        raise BatchArtifactError("selected analysis-region member bound is exceeded")
    candidate = _validated_analysis_domain_context_manifest(
        invocation, output, role="candidate_manifest", allowed_statuses=frozenset({"candidate"}))
    selected_ids = _candidate_path_object_ids(candidate)
    if len(selected_ids) != len(members):
        raise BatchArtifactError("candidate review requires only selected-region revisions")
    manifests = {"candidate_manifest": candidate}
    for role, statuses in (
        ("lineage_predecessor_candidate", frozenset({"candidate", "accepted"})),
        ("accepted_retirement_predecessor", frozenset({"accepted"})),
    ):
        previous = _validated_analysis_domain_context_manifest(
            invocation, request.get(role), role=role, allowed_statuses=statuses)
        if previous is not None:
            manifests[role] = previous
    review_context = {
        "schema_version": "analysis-domain-review-interaction/v1",
        "analysis_domain_assistance": _plain(output["assistance"]),
        "canonical_project_entry_id": authorized.project_entry_id,
        **{role: _analysis_domain_manifest_reference(invocation, value, role=role)
           for role, value in manifests.items()},
    }
    return ProjectStageCodecValidationResult(
        BatchNodeExecutionResult(authorized.attempt_authorization_sha256, ()),
        StageResult.needs_interaction(
            stage=_NODE_KIND, slide_id=authorized.slide_id,
            generation_id=output["tissue_generation_id"],
            reason=InteractionReason.ANALYSIS_DOMAIN_REVIEW_REQUIRED,
            affected_object_ids=selected_ids,
            message="Review the selected regions. Accept them to continue, or edit the selection in QuPath.",
            allowed_actions=("accept_analysis_rois", "edit_analysis_rois", "use_whole_tissue"),
            resume_context=review_context, checkpoint_id="pending-workflow-checkpoint",
        ),
    )


def build_analysis_domain_execution_result(
    evidence: ProjectStageTargetExecutionEvidence, *, state_store,
) -> BatchNodeExecutionResult | ProjectStageCodecValidationResult:
    """Return a selected-region review, or publish explicitly accepted regions."""
    context, authorized, observation = _validated_evidence_sources(evidence)
    if (
        authorized.wire_stage_id != _NODE_KIND
        or authorized.terminal_stage_id != _NODE_KIND
        or context.adapter_spec.node_kind != _NODE_KIND
    ):
        raise BatchArtifactError("accepted analysis-domain evidence has a foreign stage owner")

    inputs = tuple(context.inputs)
    if not inputs:
        raise BatchArtifactError("accepted analysis-domain evidence requires inputs")
    validation_domains = {item.scope.validation_domain_id for item in inputs}
    if None in validation_domains or len(validation_domains) != 1:
        raise BatchArtifactError(
            "accepted analysis-domain inputs require one common validation domain"
        )
    frame_refs = tuple(item for item in inputs if item.artifact_kind == "analysis_frame")
    if len(frame_refs) != 1:
        raise BatchArtifactError(
            "accepted analysis-domain evidence requires exactly one analysis_frame input"
        )
    frame_ref = frame_refs[0]
    try:
        frame = validate_analysis_frame_artifact_ref(frame_ref)
    except BatchAnalysisPreflightError as exc:
        raise BatchArtifactError("analysis_frame input is invalid") from exc
    if (
        frame_ref.scope.sample_id != authorized.sample_id
        or frame_ref.scope.slide_id != authorized.slide_id
        or frame_ref.scope.project_entry_id is not None
        or frame_ref.scope.analysis_domain_bindings
        or len(frame_ref.configuration_bindings) != 1
        or frame_ref.configuration_bindings[0].sample_id != authorized.sample_id
        or frame_ref.configuration_bindings[0].resolved_config_sha256
        != authorized.resolved_config_sha256
    ):
        raise BatchArtifactError("analysis_frame input scope or configuration differs")

    manifest = _closed_mapping(
        evidence.generation_manifest,
        _GENERATION_MANIFEST_FIELDS,
        "generation manifest",
    )
    result = evidence.wire_stage_result
    if (
        result.status is not StageStatus.COMPLETED
        or result.stage != _NODE_KIND
        or result.slide_id != authorized.slide_id
        or manifest["schema_version"] != _GENERATION_MANIFEST_SCHEMA
        or manifest["invocation_id"] != authorized.attempt_id
        or manifest["run_id"] != authorized.pipeline_run_id
        or manifest["stage"] != authorized.wire_stage_id
        or manifest["slide_id"] != authorized.slide_id
    ):
        raise BatchArtifactError("generation manifest target authority differs")
    generation_ids = tuple(
        _text(item, "generation_ids item")
        for item in _sequence(manifest["generation_ids"], "generation_ids")
    )
    if (
        len(generation_ids) != 1
        or result.generation_id is None
        or result.generation_id != generation_ids[0]
    ):
        raise BatchArtifactError("accepted analysis-domain evidence requires one exact generation")
    if (isinstance(manifest["object_outputs"], Mapping)
            and manifest["object_outputs"].get("schema_version") == "analysis-domain-retirement-attestation/v1"):
        request = evidence.reopen_validation_inputs().scientific_request_body
        outputs = _closed_mapping(manifest["object_outputs"], frozenset({
            "schema_version", "action", "canonical_image_id", "canonical_image_sha256",
            "project_entry_id", "tissue_generation_id", "accepted_retirement_predecessor_sha256",
            "successor_domain_set_sha256", "retired_source_path_object_ids", "absent_source_path_object_ids",
            "retired_annotations_locked", "audit_only", "saved_closed_acknowledged",
        }), "whole-tissue retirement output")
        current = state_store.authenticate_current_execution_context(context)
        previous = state_store.settled_review_predecessor(context, execution=current)
        if previous is None or len(previous[0].output_artifacts) != 1:
            raise BatchArtifactError("whole-tissue retirement lacks its admitted region predecessor")
        predecessor = previous[0].output_artifacts[0]
        prior_manifest = request.get("accepted_retirement_predecessor")
        if (predecessor.artifact_kind != _ARTIFACT_KIND
                or predecessor.owning_identity["selection_mode"] != _SELECTED_MODE
                or not isinstance(prior_manifest, Mapping)
                or _digest(prior_manifest) != predecessor.owning_identity["accepted_output_sha256"]
                or prior_manifest["domain_set_sha256"] != outputs["accepted_retirement_predecessor_sha256"]
                or request["action"] != "use_whole_tissue" or outputs["action"] != "use_whole_tissue"
                or outputs["canonical_image_id"] != frame.spatial_frame.canonical_image_id
                or outputs["canonical_image_sha256"] != frame.spatial_frame.canonical_image_sha256
                or outputs["project_entry_id"] != authorized.project_entry_id
                or outputs["tissue_generation_id"] != generation_ids[0]
                or outputs["retired_annotations_locked"] is not True or outputs["audit_only"] is not True
                or outputs["saved_closed_acknowledged"] is not True
                or result.outputs is None or _plain(result.outputs) != _plain(outputs)):
            raise BatchArtifactError("whole-tissue retirement differs from its exact admitted predecessor")
        retired = tuple(outputs["retired_source_path_object_ids"])
        absent = tuple(outputs["absent_source_path_object_ids"])
        expected = {row["revisions"][-1]["source_path_object_id"] for row in prior_manifest["analysis_domain_manifests"]}
        if (retired != tuple(sorted(set(retired))) or absent != tuple(sorted(set(absent)))
                or set(retired).intersection(absent) or set(retired) | set(absent) != expected):
            raise BatchArtifactError("whole-tissue retirement does not cover the exact prior region inventory")
        from .workflow import build_whole_tissue_analysis_domain_output
        selected = build_whole_tissue_analysis_domain_output(
            canonical_image_id=outputs["canonical_image_id"], canonical_image_sha256=outputs["canonical_image_sha256"],
            project_entry_id=authorized.project_entry_id, slide_id=authorized.slide_id,
            tissue_generation_id=outputs["tissue_generation_id"],
            accepted_tissue_sections=request["accepted_tissue_sections"],
            analysis_domain_assistance=request["analysis_domain_assistance"])
        manifests = tuple(AnalysisDomainManifest.from_mapping(row) for row in selected["analysis_domain_manifests"])
        overlaps = tuple(DomainOverlapEvidence.from_mapping(row) for row in selected["overlap_evidence"])
        domains = resolve_active_analysis_domain_authorities(manifests, overlaps)
        domain_set = _domain_set(domains)
        if outputs["successor_domain_set_sha256"] != domain_set.domain_set_sha256:
            raise BatchArtifactError("whole-tissue retirement successor differs from accepted tissue")
        disposition, saved_after = _saved_state(observation.saved_state_disposition, observation.saved_state_after_sha256)
        attestation_sha = _digest(outputs)
        mode = {
            "schema_version": "mif-batch-whole-tissue-retirement-evidence/v1",
            "generation_ids": list(generation_ids),
            "generation_manifest_payload_sha256": observation.generation_manifest_payload_sha256,
            "generation_manifest_byte_count": observation.generation_manifest_byte_count,
            "object_outputs_sha256": attestation_sha,
            "execution_exchange_sha256": evidence.execution_exchange_sha256,
            "validated_exchange_sha256": evidence.validated_exchange_sha256,
            "predecessor_accepted_analysis_domain_ref_sha256": predecessor.artifact_ref_sha256,
            "predecessor_domain_set_sha256": outputs["accepted_retirement_predecessor_sha256"],
            "successor_domain_set_sha256": domain_set.domain_set_sha256,
            "retirement_attestation_sha256": attestation_sha,
            "retired_source_path_object_ids": list(retired), "absent_source_path_object_ids": list(absent),
        }
        artifact = _artifact(context, selection_mode=RETIRED_WHOLE_MODE,
            sample_id=authorized.sample_id, slide_id=authorized.slide_id,
            validation_domain_id=next(iter(validation_domains)), project_entry_id=authorized.project_entry_id,
            canonical_image_id=outputs["canonical_image_id"], canonical_image_sha256=outputs["canonical_image_sha256"],
            tissue_generation_id=outputs["tissue_generation_id"], source_input_artifact_ref_sha256s=_source_ref_sha256s(inputs),
            accepted_output_sha256=_digest(selected), resolved_config_sha256=authorized.resolved_config_sha256,
            saved_state_disposition=disposition, saved_state_after_sha256=saved_after,
            assistance_sha256=_digest(request["analysis_domain_assistance"]), domain_set=domain_set,
            domain_authorities=domains, mode_evidence=mode,
            runtime_versions={"mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION,"qupath": frame_ref.runtime_versions["qupath"]})
        validate_accepted_analysis_domain_artifact_ref(artifact)
        return BatchNodeExecutionResult(authorized.attempt_authorization_sha256, (artifact,))

    object_outputs = _closed_mapping(
        manifest["object_outputs"], _SELECTED_OBJECT_FIELDS, "selected object output"
    )
    if result.outputs is None or _plain(result.outputs) != _plain(object_outputs):
        raise BatchArtifactError("wire result outputs differ from generation object_outputs")
    if object_outputs["selection_status"] == "candidate":
        return _selected_region_review(evidence, object_outputs, frame)
    if (
        object_outputs["schema_version"] != _SELECTED_OBJECT_SCHEMA
        or object_outputs["selection_status"] != "accepted"
        or object_outputs["saved_closed_acknowledged"] is not True
        or object_outputs["selected_annotations_locked"] is not True
    ):
        raise BatchArtifactError("selected object output is not accepted and closed")
    assistance = object_outputs["assistance"]
    if not isinstance(assistance, Mapping) or not assistance:
        raise BatchArtifactError("selected object assistance must be a nonempty object")
    _canonical_bytes(assistance)

    spatial_frame = frame.spatial_frame
    canonical_image_id = _text(object_outputs["canonical_image_id"], "canonical_image_id")
    canonical_image_sha256 = _sha256(
        object_outputs["canonical_image_sha256"], "canonical_image_sha256"
    )
    project_entry_id = _text(object_outputs["project_entry_id"], "project_entry_id")
    tissue_generation_id = _text(object_outputs["tissue_generation_id"], "tissue_generation_id")
    if (
        canonical_image_id != spatial_frame.canonical_image_id
        or canonical_image_sha256 != spatial_frame.canonical_image_sha256
        or project_entry_id != authorized.project_entry_id
        or tissue_generation_id != generation_ids[0]
    ):
        raise BatchArtifactError("selected object image, project, or generation differs")

    manifest_values = _sequence(
        object_outputs["analysis_domain_manifests"], "analysis_domain_manifests"
    )
    overlap_values = _sequence(object_outputs["overlap_evidence"], "overlap_evidence")
    if not manifest_values or len(manifest_values) > MAX_ACCEPTED_ANALYSIS_DOMAIN_MEMBERS:
        raise BatchArtifactError("accepted analysis-domain member bound is exceeded")
    try:
        domain_manifests = tuple(
            AnalysisDomainManifest.from_mapping(_plain(item)) for item in manifest_values
        )
        overlaps = tuple(
            DomainOverlapEvidence.from_mapping(_plain(item)) for item in overlap_values
        )
        for domain_manifest in domain_manifests:
            if (
                domain_manifest.canonical_image_id != canonical_image_id
                or domain_manifest.canonical_image_sha256 != canonical_image_sha256
                or domain_manifest.project_entry_id != project_entry_id
                or domain_manifest.slide_id != authorized.slide_id
                or domain_manifest.tissue_generation_id != tissue_generation_id
                or len({item.analysis_domain_id for item in domain_manifest.revisions}) != 1
            ):
                raise BatchArtifactError(
                    "analysis-domain manifest scope or stable-domain coverage differs"
                )
        for overlap in overlaps:
            if (
                overlap.canonical_image_id != canonical_image_id
                or overlap.canonical_image_sha256 != canonical_image_sha256
                or overlap.project_entry_id != project_entry_id
                or overlap.tissue_generation_id != tissue_generation_id
            ):
                raise BatchArtifactError("overlap evidence scope differs")
        for domain_manifest, overlap in zip(domain_manifests, overlaps, strict=True):
            validate_domain_manifest(domain_manifest, overlap)
        domain_authorities = resolve_active_analysis_domain_authorities(domain_manifests, overlaps)
    except (AnalysisDomainError, TypeError, ValueError) as exc:
        raise BatchArtifactError("selected raw domain evidence is invalid") from exc
    if (
        len(domain_authorities) != len(domain_manifests)
        or len(domain_authorities) > MAX_ACCEPTED_ANALYSIS_DOMAIN_MEMBERS
        or any(
            item.analysis_domain_kind is not AnalysisDomainKind.SELECTED_ROI
            for item in domain_authorities
        )
    ):
        raise BatchArtifactError(
            "accepted analysis-domain evidence must contain selected revisions only"
        )
    domain_set = _domain_set(domain_authorities)
    if object_outputs["domain_set_sha256"] != domain_set.domain_set_sha256:
        raise BatchArtifactError("raw domain-set digest differs from resolved authorities")

    manifest_digest = _sha256(
        observation.generation_manifest_payload_sha256,
        "generation_manifest_payload_sha256",
    )
    manifest_count = _positive_integer(
        observation.generation_manifest_byte_count,
        "generation_manifest_byte_count",
    )
    saved_state_disposition, saved_state_after_sha256 = _saved_state(
        observation.saved_state_disposition,
        observation.saved_state_after_sha256,
    )
    source_refs = _source_ref_sha256s(inputs)
    if source_refs != authorized.prerequisite_artifact_ref_sha256s:
        raise BatchArtifactError(
            "accepted analysis-domain source inputs differ from Task-10 authority"
        )
    mode_evidence = {
        "schema_version": "mif-batch-selected-analysis-domain-evidence/v1",
        "generation_ids": list(generation_ids),
        "generation_manifest_payload_sha256": manifest_digest,
        "generation_manifest_byte_count": manifest_count,
        "object_outputs_sha256": _digest(object_outputs),
        "execution_exchange_sha256": evidence.execution_exchange_sha256,
        "validated_exchange_sha256": evidence.validated_exchange_sha256,
    }
    output = _artifact(
        context,
        selection_mode=_SELECTED_MODE,
        sample_id=authorized.sample_id,
        slide_id=authorized.slide_id,
        validation_domain_id=next(iter(validation_domains)),
        project_entry_id=project_entry_id,
        canonical_image_id=canonical_image_id,
        canonical_image_sha256=canonical_image_sha256,
        tissue_generation_id=tissue_generation_id,
        source_input_artifact_ref_sha256s=source_refs,
        accepted_output_sha256=_digest(object_outputs),
        resolved_config_sha256=authorized.resolved_config_sha256,
        saved_state_disposition=saved_state_disposition,
        saved_state_after_sha256=saved_state_after_sha256,
        assistance_sha256=_digest(assistance),
        domain_set=domain_set,
        domain_authorities=domain_authorities,
        mode_evidence=mode_evidence,
        runtime_versions={
            "mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION,
            "qupath": frame_ref.runtime_versions["qupath"],
        },
    )
    return BatchNodeExecutionResult(authorized.attempt_authorization_sha256, (output,))


def reopen_committed_accepted_analysis_domain_live_authority(
    authority: BatchCommittedArtifactAuthority,
    *,
    accepted_tissue: ValidatedAcceptedTissueLiveAuthority,
    exchange_reopener: ProjectStageCommittedExchangeReopener,
) -> ValidatedAcceptedAnalysisDomainLiveAuthority:
    """Reopen the exact native manifests under the current overlap policy.

    Selected regions retain their native exchange, manifest and accepted-output
    identities. Their validation stamp also includes the issuing context, so it
    cannot serve as native-result identity across failed-result recovery. The
    reopener authenticates that original producer and its exact exchange bytes;
    we compare the saved scientific output and tissue association below.
    """

    committed = decode_committed_accepted_analysis_domain(authority)
    tissue = _validate_accepted_tissue_live_authority(accepted_tissue)
    if committed.selection_mode in {"whole_tissue_direct", RETIRED_WHOLE_MODE}:
        from .workflow import build_whole_tissue_analysis_domain_output

        source_tissue = tissue.committed
        if (
            source_tissue.artifact_ref.artifact_ref_sha256
            not in committed.source_input_artifact_ref_sha256s
            or source_tissue.state_head_sha256 != committed.state_head_sha256
            or source_tissue.active_plan_sha256 != committed.active_plan_sha256
            or source_tissue.sample_id != committed.sample_id
            or source_tissue.project_entry_id != committed.project_entry_id
            or source_tissue.canonical_image_sha256 != committed.canonical_image_sha256
            or source_tissue.generation_id != committed.tissue_generation_id
        ):
            raise BatchArtifactError("Whole-section selection differs from its accepted tissue")
        # Reconstruct the reference-only manifests from the accepted tissue rows.
        # Their domain and geometry identities are compared with the saved selection.
        output = build_whole_tissue_analysis_domain_output(
            canonical_image_id=committed.canonical_image_id,
            canonical_image_sha256=committed.canonical_image_sha256,
            project_entry_id=committed.project_entry_id, slide_id=committed.slide_id,
            tissue_generation_id=committed.tissue_generation_id,
            accepted_tissue_sections=tissue.accepted_tissue_request_rows,
            analysis_domain_assistance={
                "schema_version": "analysis-domain-assistance/v1", "mode": "unavailable",
                "canonical_project_entry_id": committed.project_entry_id,
                "annotation_project_entry_id": committed.project_entry_id,
                "coordinate_system": "global_level0_pixels", "coordinate_transfer_authorized": False,
            },
        )
        manifests = tuple(AnalysisDomainManifest.from_mapping(row) for row in output["analysis_domain_manifests"])
        overlaps = tuple(DomainOverlapEvidence.from_mapping(row) for row in output["overlap_evidence"])
        domains = resolve_active_analysis_domain_authorities(manifests, overlaps)
        domain_set = _domain_set(domains)
        if domains != committed.domain_authorities or domain_set != committed.domain_set:
            raise BatchArtifactError("Whole-section domain references differ from their saved selection")
        if committed.selection_mode == RETIRED_WHOLE_MODE:
            ref = authority.artifact_ref
            retained = exchange_reopener._reopen_for_owner(authority,
                producer_artifact_ref_sha256=ref.artifact_ref_sha256, producer_node_id=ref.producer_node_id,
                producer_attempt_id=ref.producer_attempt_id,
                producer_attempt_authorization_sha256=ref.producer_attempt_authorization_sha256,
                terminal_record_sha256=committed.terminal_record_sha256,
                execution_exchange_sha256=committed.execution_exchange_sha256,
                expected_binding=(QuPathScriptAsset.SNAPSHOT_ANALYSIS_DOMAINS.value,_NODE_KIND,_NODE_KIND,"1"))
            evidence = retained.evidence
            mode = committed.mode_evidence
            if (evidence.wire_stage_result.status is not StageStatus.COMPLETED
                    or _digest(evidence.generation_manifest["object_outputs"]) != mode["retirement_attestation_sha256"]
                    or evidence.execution_exchange_sha256 != mode["execution_exchange_sha256"]
                    or evidence.validated_exchange_sha256 != mode["validated_exchange_sha256"]):
                raise BatchArtifactError("Whole-section retirement differs from its recorded native result")
        value = object.__new__(ValidatedAcceptedAnalysisDomainLiveAuthority)
        for name, item in {
            "committed": committed, "accepted_tissue": tissue, "manifests": manifests,
            "overlap_evidence": overlaps, "accepted_tissue_request_rows": tissue.accepted_tissue_request_rows,
            "domain_authorities": domains, "domain_set": domain_set,
            "_issuer": _ACCEPTED_ANALYSIS_DOMAIN_LIVE_ISSUER,
        }.items():
            object.__setattr__(value, name, item)
        return value
    if (
        committed.selection_mode != _SELECTED_MODE
        or type(exchange_reopener) is not ProjectStageCommittedExchangeReopener
        or committed.execution_exchange_sha256 is None
    ):
        raise BatchArtifactError("selected analysis-domain reopen requires exact live authorities")
    artifact_ref = authority.artifact_ref
    if (
        artifact_ref.producer_node_id is None
        or artifact_ref.producer_attempt_id is None
        or artifact_ref.producer_attempt_authorization_sha256 is None
    ):
        raise BatchArtifactError("selected analysis-domain artifact lacks exact producer identity")
    try:
        reopened = exchange_reopener._reopen_for_owner(
            authority,
            producer_artifact_ref_sha256=artifact_ref.artifact_ref_sha256,
            producer_node_id=artifact_ref.producer_node_id,
            producer_attempt_id=artifact_ref.producer_attempt_id,
            producer_attempt_authorization_sha256=(
                artifact_ref.producer_attempt_authorization_sha256
            ),
            terminal_record_sha256=committed.terminal_record_sha256,
            execution_exchange_sha256=committed.execution_exchange_sha256,
            expected_binding=(
                QuPathScriptAsset.SNAPSHOT_ANALYSIS_DOMAINS.value,
                _NODE_KIND,
                _NODE_KIND,
                "1",
            ),
        )
        evidence = reopened.evidence
        context, authorized, observation = _validated_evidence_sources(evidence)
    except (ProjectStageRecordError, TypeError, ValueError) as exc:
        raise BatchArtifactError(
            "selected analysis-domain retained exchange cannot be reopened"
        ) from exc
    manifest = _closed_mapping(
        evidence.generation_manifest,
        _GENERATION_MANIFEST_FIELDS,
        "retained generation manifest",
    )
    result = evidence.wire_stage_result
    generation_ids = tuple(
        _text(item, "retained generation_ids item")
        for item in _sequence(manifest["generation_ids"], "generation_ids")
    )
    mode = committed.mode_evidence
    if (
        result.status is not StageStatus.COMPLETED
        or result.stage != _NODE_KIND
        or result.slide_id != committed.slide_id
        or manifest["schema_version"] != _GENERATION_MANIFEST_SCHEMA
        or manifest["invocation_id"] != authorized.attempt_id
        or manifest["run_id"] != authorized.pipeline_run_id
        or manifest["stage"] != authorized.wire_stage_id
        or manifest["slide_id"] != authorized.slide_id
        or len(generation_ids) != 1
        or result.generation_id != generation_ids[0]
        or tuple(mode.get("generation_ids", ())) != generation_ids
        or observation.generation_manifest_payload_sha256
        != mode.get("generation_manifest_payload_sha256")
        or observation.generation_manifest_byte_count != mode.get("generation_manifest_byte_count")
        or evidence.execution_exchange_sha256 != mode.get("execution_exchange_sha256")
    ):
        raise BatchArtifactError("selected analysis-domain retained target authority differs")
    object_outputs = _closed_mapping(
        manifest["object_outputs"],
        _SELECTED_OBJECT_FIELDS,
        "retained selected object output",
    )
    if (
        result.outputs is None
        or _plain(result.outputs) != _plain(object_outputs)
        or object_outputs["schema_version"] != _SELECTED_OBJECT_SCHEMA
        or object_outputs["selection_status"] != "accepted"
        or object_outputs["saved_closed_acknowledged"] is not True
        or object_outputs["selected_annotations_locked"] is not True
        or _digest(object_outputs) != mode.get("object_outputs_sha256")
        or _digest(object_outputs) != committed.accepted_output_sha256
        or object_outputs["canonical_image_id"] != committed.canonical_image_id
        or object_outputs["canonical_image_sha256"] != committed.canonical_image_sha256
        or object_outputs["project_entry_id"] != committed.project_entry_id
        or object_outputs["tissue_generation_id"] != committed.tissue_generation_id
        or _digest(object_outputs["assistance"]) != committed.assistance_sha256
    ):
        raise BatchArtifactError("selected analysis-domain compact and retained output differ")
    raw_manifests = _sequence(
        object_outputs["analysis_domain_manifests"],
        "analysis_domain_manifests",
    )
    raw_overlaps = _sequence(object_outputs["overlap_evidence"], "overlap_evidence")
    if (
        not raw_manifests
        or len(raw_manifests) > MAX_ACCEPTED_ANALYSIS_DOMAIN_MEMBERS
        or len(raw_manifests) != len(raw_overlaps)
    ):
        raise BatchArtifactError("selected analysis-domain retained inventory is invalid")
    try:
        manifests = tuple(
            AnalysisDomainManifest.from_mapping(_plain(item)) for item in raw_manifests
        )
        overlaps = tuple(DomainOverlapEvidence.from_mapping(_plain(item)) for item in raw_overlaps)
        if any(
            _plain(raw) != manifest_item.to_mapping()
            for raw, manifest_item in zip(raw_manifests, manifests, strict=True)
        ) or any(
            _plain(raw) != overlap.to_mapping()
            for raw, overlap in zip(raw_overlaps, overlaps, strict=True)
        ):
            raise AnalysisDomainError("retained analysis-domain wire rows changed after parsing")
        domain_authorities = resolve_active_analysis_domain_authorities(manifests, overlaps)
    except (AnalysisDomainError, TypeError, ValueError) as exc:
        raise BatchArtifactError("selected analysis-domain retained science is invalid") from exc
    domain_set = _domain_set(domain_authorities)
    if (
        domain_authorities != committed.domain_authorities
        or domain_set != committed.domain_set
        or object_outputs["domain_set_sha256"] != domain_set.domain_set_sha256
        or any(
            item.analysis_domain_kind is not AnalysisDomainKind.SELECTED_ROI
            for item in domain_authorities
        )
    ):
        raise BatchArtifactError(
            "selected analysis-domain compact authority differs from full histories"
        )
    tissue_inputs = tuple(
        item for item in context.inputs if item.artifact_kind == "tissue_acceptance_result"
    )
    if (
        len(tissue_inputs) != 1
        or _source_ref_sha256s(context.inputs) != committed.source_input_artifact_ref_sha256s
        or tissue_inputs[0].artifact_ref_sha256 != tissue.committed.artifact_ref.artifact_ref_sha256
        or tissue_inputs[0].artifact_ref_sha256 not in committed.source_input_artifact_ref_sha256s
        or tissue.committed.state_head_sha256 != committed.state_head_sha256
        or tissue.committed.active_plan_sha256 != committed.active_plan_sha256
        or tissue.committed.sample_id != committed.sample_id
        or tissue.committed.slide_id != committed.slide_id
        or tissue.committed.project_entry_id != committed.project_entry_id
        or tissue.committed.canonical_image_id != committed.canonical_image_id
        or tissue.committed.canonical_image_sha256 != committed.canonical_image_sha256
        or tissue.generation.generation_id != committed.tissue_generation_id
    ):
        raise BatchArtifactError("selected analysis-domain accepted tissue scope differs")
    tissue_by_section = {item.section.section_id: item for item in tissue.sections}
    seen_manifest_ids: set[str] = set()
    for domain_manifest in manifests:
        section = tissue_by_section.get(domain_manifest.section_id)
        if (
            section is None
            or domain_manifest.content_sha256 in seen_manifest_ids
            or domain_manifest.slide_id != section.section.slide_id
            or domain_manifest.tissue_generation_id != section.section.generation_id
            or domain_manifest.tissue_roi_id != section.section.roi_id
            or domain_manifest.tissue_path_object_id != section.section.path_object_id
            or domain_manifest.canonical_image_id != committed.canonical_image_id
            or domain_manifest.canonical_image_sha256 != committed.canonical_image_sha256
            or domain_manifest.project_entry_id != committed.project_entry_id
        ):
            raise BatchArtifactError(
                "selected analysis-domain manifest lies outside accepted tissue"
            )
        seen_manifest_ids.add(domain_manifest.content_sha256)
    rows = tissue.accepted_tissue_request_rows
    value = object.__new__(ValidatedAcceptedAnalysisDomainLiveAuthority)
    for name, item in {
        "committed": committed,
        "accepted_tissue": tissue,
        "manifests": manifests,
        "overlap_evidence": overlaps,
        "accepted_tissue_request_rows": rows,
        "domain_authorities": domain_authorities,
        "domain_set": domain_set,
        "_issuer": _ACCEPTED_ANALYSIS_DOMAIN_LIVE_ISSUER,
    }.items():
        object.__setattr__(value, name, item)
    return value


def _validate_mode_neutral_prerequisites(
    context: BatchNodeExecutionContext,
    *,
    sample_id: str,
    slide_id: str,
    project_entry_id: str,
    resolved_config_sha256: str,
    source_ref_sha256s: tuple[str, ...],
) -> None:
    """Authenticate available input owners without guessing registration fields.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    inputs = tuple(context.inputs)
    requirements = tuple(context.adapter_spec.required_inputs)
    expected_kinds = {
        "analysis_frame",
        "registration_result",
        "tissue_acceptance_result",
    }
    if (
        len(inputs) != 3
        or len(requirements) != 3
        or {item.artifact_kind for item in requirements} != expected_kinds
        or any(item.multiplicity is not ArtifactMultiplicity.SINGLE for item in requirements)
        or {item.artifact_kind for item in inputs} != expected_kinds
        or len({item.artifact_kind for item in inputs}) != 3
        or _source_ref_sha256s(inputs) != source_ref_sha256s
    ):
        raise UnsupportedBatchAnalysisDomainProducerError(
            "mode-neutral analysis-domain production requires exact analysis_frame, "
            "registration_result, and tissue_acceptance_result inputs"
        )
    validation_domains = {item.scope.validation_domain_id for item in inputs}
    if (
        None in validation_domains
        or len(validation_domains) != 1
        or any(
            item.scope.sample_id != sample_id
            or item.scope.slide_id != slide_id
            or len(item.configuration_bindings) != 1
            or item.configuration_bindings[0].sample_id != sample_id
            or item.configuration_bindings[0].resolved_config_sha256 != resolved_config_sha256
            for item in inputs
        )
    ):
        raise BatchArtifactError("mode-neutral analysis-domain prerequisite scope differs")
    by_kind = {item.artifact_kind: item for item in inputs}
    frame_ref = by_kind["analysis_frame"]
    tissue_ref = by_kind["tissue_acceptance_result"]
    if (
        frame_ref.scope.project_entry_id is not None
        or frame_ref.scope.analysis_domain_bindings
        or tissue_ref.scope.project_entry_id != project_entry_id
        or tissue_ref.scope.analysis_domain_bindings
    ):
        raise BatchArtifactError("mode-neutral analysis-domain prerequisite owner scope differs")
    try:
        frame = validate_analysis_frame_artifact_ref(frame_ref)
        tissue = validate_tissue_acceptance_result_artifact_ref(tissue_ref)
    except (BatchAnalysisPreflightError, BatchArtifactError) as exc:
        raise BatchArtifactError(
            "mode-neutral analysis-domain prerequisite owner is invalid"
        ) from exc
    if (
        tissue.sample_id != sample_id
        or tissue.slide_id != slide_id
        or tissue.project_entry_id != project_entry_id
        or tissue.canonical_image_id != frame.spatial_frame.canonical_image_id
        or tissue.canonical_image_sha256 != frame.spatial_frame.canonical_image_sha256
        or tissue.generation_status != "accepted"
        or tissue.authoritative_accepted_generation_id != tissue.generation_id
        or not tissue.section_inventory
    ):
        raise BatchArtifactError("mode-neutral tissue-acceptance authority differs")


def build_direct_whole_tissue_analysis_domain_execution_result(
    context: BatchNodeExecutionContext,
    *,
    current_execution_authority: BatchCurrentExecutionAuthority,
    state_store,
    workspace_lease,
) -> BatchNodeExecutionResult:
    """Select the complete accepted tissue through its existing boundary references.

    We resolve the committed tissue and registration, compare their saved image
    state with the current project, and derive one whole-section domain per
    accepted section. The output keeps those boundary identities and records this
    selection against the current operation; image and hierarchy files are reused.
    """

    if type(context) is not BatchNodeExecutionContext:
        raise BatchArtifactError(
            "direct whole-tissue composition requires an exact execution context"
        )
    try:
        current = validate_current_execution_authority(
            current_execution_authority,
            expected_context=context,
        )
    except (AttributeError, BatchArtifactError, TypeError, ValueError) as exc:
        raise BatchArtifactError(
            "direct whole-tissue current execution authority is invalid"
        ) from exc
    dispatch = context.authorization.dispatch_binding
    contract = accepted_analysis_domain_identity_contract()
    if (
        context.adapter_spec.node_kind != _NODE_KIND
        or context.node_id != context.authorization.node_id
        or context.adapter_spec.declared_outputs != (_ARTIFACT_KIND,)
        or context.adapter_spec.output_identity_contracts != (contract,)
        or current.node_kind != _NODE_KIND
        or current.node_scope != "sample"
        or current.clustering_scope is not None
        or current.validation_domain_id is None
        or len(current.participant_ids) != 1
        or len(current.project_entry_ids) != 1
        or current.analysis_domain_bindings
        or len(current.configuration_bindings) != 1
        or current.participant_ids != dispatch.participant_ids
        or current.project_entry_ids != dispatch.project_entry_ids
    ):
        raise BatchArtifactError("direct whole-tissue execution authority scope differs")
    slide_ids = {item.scope.slide_id for item in context.inputs}
    if None in slide_ids or len(slide_ids) != 1:
        raise BatchArtifactError("direct whole-tissue inputs require one exact slide")
    _validate_mode_neutral_prerequisites(
        context,
        sample_id=current.participant_ids[0],
        slide_id=next(iter(slide_ids)),
        project_entry_id=current.project_entry_ids[0],
        resolved_config_sha256=(current.configuration_bindings[0].resolved_config_sha256),
        source_ref_sha256s=current.input_artifact_ref_sha256s,
    )
    from .batch_tissue_artifacts import decode_committed_tissue_acceptance_result
    from .batch_registration import decode_committed_registration_result
    from .accepted_analysis_domain_records import DIRECT_WHOLE_MODE
    from .batch_artifacts import artifact_ref_set_sha256
    from .workflow import build_whole_tissue_analysis_domain_output

    # i) Resolve the accepted section and paired-image decision through their
    # producing records. Their saved hierarchy must still be the selected one.
    by_kind = {item.artifact_kind: item for item in context.inputs}
    tissue = decode_committed_tissue_acceptance_result(
        state_store.authenticate_available_artifact(by_kind["tissue_acceptance_result"])
    )
    registration = decode_committed_registration_result(
        state_store.authenticate_available_artifact(by_kind["registration_result"])
    )
    saved = state_store.current_project_entry_saved_state_sha256_by_id(
        expected_head_sha256=current.state_head_sha256, lease=workspace_lease,
    )
    from .batch_settled_review_revision import revision_root
    root = revision_root(state_store._load_plan_snapshot(current.active_plan_sha256), context.node_id)
    region_return = root is not None and root["kind"] == "analysis_domain_selection"
    alignment_return = (root is not None and root["kind"] == "registration"
                        and registration.artifact_ref.producer_node_id == root["node_id"])
    if region_return or alignment_return:
        state_store.settled_review_predecessor(context, execution=current)
    if (
        registration.sample_id != tissue.sample_id
        or registration.slide_id != tissue.slide_id
        or registration.primary_project_entry_id != tissue.project_entry_id
        or registration.primary_image_sha256 != tissue.canonical_image_sha256
        or registration.tissue_generation_id != tissue.generation_id
        or registration.artifact_ref.owning_identity["tissue_acceptance_result_ref_sha256"]
        != by_kind["tissue_acceptance_result"].artifact_ref_sha256
        or (not (region_return or alignment_return)
            and saved.get(tissue.project_entry_id) != tissue.saved_state_after_sha256)
        or (not region_return
            and registration.artifact_ref.owning_identity["primary_saved_state_attestation_sha256"]
            != saved.get(tissue.project_entry_id))
    ):
        raise BatchArtifactError("Accepted tissue or its saved image changed before region selection")

    # ii) A whole-section domain references the accepted native tissue boundary.
    # The existing scientific constructor establishes the domain ID and revision.
    assistance = {
        "schema_version": "analysis-domain-assistance/v1", "mode": "unavailable",
        "canonical_project_entry_id": tissue.project_entry_id,
        "annotation_project_entry_id": tissue.project_entry_id,
        "coordinate_system": "global_level0_pixels", "coordinate_transfer_authorized": False,
    }
    if registration.mode == "side_by_side":
        assistance.update(mode="side_by_side", moving_project_entry_id=registration.moving_project_entry_id)
    rows = tuple({
        "section_id": section["section_id"], "tissue_roi_id": section["roi_id"],
        "tissue_path_object_id": section["path_object_id"],
        "tissue_generation_id": tissue.generation_id,
    } for section in tissue.section_inventory)
    selected = build_whole_tissue_analysis_domain_output(
        canonical_image_id=tissue.canonical_image_id,
        canonical_image_sha256=tissue.canonical_image_sha256,
        project_entry_id=tissue.project_entry_id, slide_id=tissue.slide_id,
        tissue_generation_id=tissue.generation_id, accepted_tissue_sections=rows,
        analysis_domain_assistance=assistance,
    )
    manifests = tuple(AnalysisDomainManifest.from_mapping(row) for row in selected["analysis_domain_manifests"])
    overlaps = tuple(DomainOverlapEvidence.from_mapping(row) for row in selected["overlap_evidence"])
    domains = resolve_active_analysis_domain_authorities(manifests, overlaps)
    domain_set = _domain_set(domains)

    # iii) Publish the selected domain references with the operation's inputs.
    # The native project and its cell/annotation hierarchy retain their bytes.
    mode_evidence = {
        "schema_version": "mif-batch-whole-tissue-direct-evidence/v1",
        "attempt_authorization_sha256": current.authorization_sha256,
        "input_artifact_set_sha256": artifact_ref_set_sha256(context.inputs),
        "node_material_sha256": current.node_material_sha256,
        "dispatch_binding_sha256": current.dispatch_binding_sha256,
        "execution_context_sha256": current.execution_context_sha256,
        "current_state_head_sha256": current.state_head_sha256,
        "active_plan_sha256": current.active_plan_sha256,
    }
    output = _artifact(
        context, selection_mode=DIRECT_WHOLE_MODE,
        sample_id=tissue.sample_id, slide_id=tissue.slide_id,
        validation_domain_id=current.validation_domain_id,
        project_entry_id=tissue.project_entry_id, canonical_image_id=tissue.canonical_image_id,
        canonical_image_sha256=tissue.canonical_image_sha256,
        tissue_generation_id=tissue.generation_id,
        source_input_artifact_ref_sha256s=current.input_artifact_ref_sha256s,
        accepted_output_sha256=_digest(selected),
        resolved_config_sha256=current.configuration_bindings[0].resolved_config_sha256,
        saved_state_disposition="authenticated_unchanged",
        saved_state_after_sha256=saved[tissue.project_entry_id],
        assistance_sha256=_digest(assistance), domain_set=domain_set,
        domain_authorities=domains, mode_evidence=mode_evidence,
        runtime_versions={"mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION},
    )
    validate_accepted_analysis_domain_artifact_ref(output)
    return BatchNodeExecutionResult(current.authorization_sha256, (output,))


__all__ = [
    "ACCEPTED_ANALYSIS_DOMAIN_IDENTITY_FIELDS",
    "ACCEPTED_ANALYSIS_DOMAIN_OPTIONAL_IDENTITY_FIELDS",
    "MAX_ACCEPTED_ANALYSIS_DOMAIN_MEMBERS",
    "UnsupportedBatchAnalysisDomainProducerError",
    "ValidatedAcceptedAnalysisDomainLiveAuthority",
    "ValidatedAcceptedAnalysisDomainArtifactRefProjection",
    "ValidatedAcceptedAnalysisDomainProjection",
    "accepted_analysis_domain_identity_contract",
    "build_analysis_domain_execution_result",
    "build_direct_whole_tissue_analysis_domain_execution_result",
    "decode_committed_accepted_analysis_domain",
    "reopen_committed_accepted_analysis_domain_live_authority",
    "validate_accepted_analysis_domain_artifact_ref",
]
