"""and direct adapters for the applicable semantic batch chain.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from ..batch_manifest import operational_sample_output_root

from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
import hashlib
from pathlib import Path

from mif_pipeline.cell_analysis import (
    FinalizedSecondaryDomainEvidence,
    ManualSelectionEvidence,
)
from mif_pipeline.compartment_analysis import (
    ManualSelectionLineage,
    SelectionAlgorithm,
)
from mif_pipeline.orchestration.batch_analysis_preflight import (
    validate_analysis_frame_artifact_ref,
)
from mif_pipeline.orchestration.batch_artifacts import (
    ArtifactMultiplicity,
    BatchArtifactError,
    BatchCommittedArtifactAuthority,
    BatchNodeExecutionContext,
    BatchNodeExecutionResult,
    DispatchMode,
    StageAdapterSpec,
)
from mif_pipeline.orchestration.batch_interactions import (
    INTERACTION_RESPONSE_SCHEMA_NAME,
    INTERACTION_RESPONSE_SCHEMA_VERSION,
    BatchInteractionTarget,
    BatchTargetResponseValidation,
)
from mif_pipeline.orchestration.batch_manifest import ResolvedBatchSample
from mif_pipeline.orchestration.batch_state import (
    BatchAttemptAuthorizationRequest,
    BatchRawNodeOutcome,
    BatchStateError,
    BatchStateStore,
    batch_interaction_request_id,
)
from mif_pipeline.orchestration.project_stage_codecs import (
    ProjectStageCodec,
    ProjectStageCodecBinding,
    ProjectStageCodecError,
    ProjectStageTargetIntent,
)
from mif_pipeline.orchestration.project_stage_owner_capabilities import (
    ProjectStageOwnerCapabilityError,
    ProjectStageOwnerCapabilityResolver,
    SemanticEvidenceStageOwnerCapability,
    SemanticFinalizationStageOwnerCapability,
    _validate_applicable_capability,
)
from mif_pipeline.orchestration.project_stage_plan import (
    ProjectStageTargetExecutionEvidence,
)
from mif_pipeline.orchestration.semantic_execution import (
    build_roi_semantic_manual_cap_interaction,
    evaluate_roi_semantic_result_set,
    publish_roi_semantic_result_artifact,
    resolve_roi_semantic_count,
    resolve_roi_terminal_semantic_review,
    validate_roi_semantic_evidence_response,
    validate_roi_semantic_finalization_response,
    validate_roi_semantic_manual_cap_response,
    validate_roi_semantic_raw_inventory,
    validate_roi_terminal_semantic_review_response,
)
from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from mif_pipeline.pipeline_core.interactions import (
    resolve_resume_envelope,
    validate_interaction_response,
)
from mif_pipeline.pipeline_core.stage_result import (
    InteractionReason,
    StageResult,
    StageStatus,
)
from mif_pipeline.qupath_runtime.scripts import QuPathScriptAsset
from mif_pipeline.roi_validation import SemanticRegionDecision

from ._common import BatchPhenotypingError, digest, plain_json, read_bounded_file
from .application_codec import decode_committed_semantic_raw_inventory
from .semantic_applicable import (
    ValidatedFinalCountCapResult,
    ValidatedSemanticEvaluationResult,
    _count_inputs,
    _decode_semantic_evaluation_for_count,
    _decode_semantic_evidence_for_evaluation,
    _profile_owner,
    build_final_count_cap_execution_result,
    build_semantic_evaluation_execution_result,
    build_semantic_evidence_execution_result,
    build_semantic_finalization_execution_result,
    final_count_cap_result_identity_contract,
    semantic_evaluation_result_identity_contract,
    semantic_evidence_result_identity_contract,
    semantic_finalization_result_identity_contract,
)

_CODEC_VERSION = "1"
_WIRE_STAGE = "semantic_validation"
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
_EVIDENCE_OUTPUT_FIELDS = frozenset(
    {
        "path",
        "artifact_sha256",
        "schema_version",
        "candidate_count",
        "cell_count",
        "coordinate_system",
    }
)
_FINAL_OUTPUT_FIELDS = frozenset(
    {
        "component_descriptor",
        "updated_roi_ids",
        "roi_count",
        "section_id",
        "coordinate_system",
        "review_status",
        "review_action",
        "reconciled_cell_ids_sha256",
        "cell_count",
        "semantic_finalization",
    }
)
_EVALUATION_VALIDATOR_ID = "mif-batch-semantic-evaluation-response/v1"
_COUNT_VALIDATOR_ID = "mif-batch-final-count-cap-response/v1"


# ==============================================================================
#                               Semantic adapters
# ==============================================================================


def _closed_mapping(value: object, fields: frozenset[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise BatchPhenotypingError(f"{label} does not have its exact closed fields")
    return value


def _sequence(value: object, label: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise BatchPhenotypingError(f"{label} must be an array")
    return tuple(value)


def _stage_spec(
    spec: StageAdapterSpec,
    *,
    node_kind: str,
    output_kind: str,
    contract: object,
    dispatch: DispatchMode,
    required: Mapping[str, ArtifactMultiplicity],
) -> None:
    if (
        type(spec) is not StageAdapterSpec
        or spec.node_kind != node_kind
        or spec.node_version != "1"
        or spec.dispatch_mode is not dispatch
        or spec.declared_outputs != (output_kind,)
        or spec.output_identity_contracts != (contract,)
        or {item.artifact_kind: item.multiplicity for item in spec.required_inputs}
        != dict(required)
        or spec.optional_inputs
    ):
        raise ProjectStageCodecError(f"{node_kind} adapter specification differs")


def _evidence_spec(spec: StageAdapterSpec) -> None:
    if (
        type(spec) is not StageAdapterSpec
        or spec.node_kind != "semantic_evidence"
        or spec.node_version != "1"
        or spec.dispatch_mode is not DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE
        or spec.resource_kind != "qupath_image_write"
        or spec.declared_outputs != ("semantic_evidence_result",)
        or spec.output_identity_contracts != (semantic_evidence_result_identity_contract(),)
        or {item.artifact_kind: item.multiplicity for item in spec.required_inputs}
        != {
            "semantic_raw_inventory": ArtifactMultiplicity.SINGLE,
            "analysis_frame": ArtifactMultiplicity.SINGLE,
            "accepted_analysis_domain": ArtifactMultiplicity.SINGLE,
            "measurements_result": ArtifactMultiplicity.SINGLE,
            "semantic_profile": ArtifactMultiplicity.EXACT_SET,
        }
        or {item.artifact_kind: item.multiplicity for item in spec.optional_inputs}
        != {
            "marker_phenotype_result": ArtifactMultiplicity.SINGLE,
            "supervised_phenotype_result": ArtifactMultiplicity.SINGLE,
            "cluster_label_import_result": ArtifactMultiplicity.SINGLE,
            "phenotype_view_attestation": ArtifactMultiplicity.SINGLE,
        }
    ):
        raise ProjectStageCodecError("semantic evidence adapter specification differs")


def _resolve_capability(
    resolver: ProjectStageOwnerCapabilityResolver,
    context: BatchNodeExecutionContext,
    *,
    project_entry_id: str,
    binding: object,
    cls: type,
) -> object:
    try:
        value = resolver._resolve_for_target(
            context,
            project_entry_id=project_entry_id,
            analysis_domain_binding=binding,
        )
        value = _validate_applicable_capability(value, cls)
    except (ProjectStageOwnerCapabilityError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("applicable semantic capability differs") from exc
    return value


def _manifest(
    evidence: ProjectStageTargetExecutionEvidence,
    *,
    outputs: Mapping[str, object],
    generation_id: str,
) -> None:
    manifest = _closed_mapping(
        evidence.generation_manifest,
        _MANIFEST_FIELDS,
        "semantic generation manifest",
    )
    authorized = evidence.authorized
    if (
        manifest["schema_version"] != "roi-generation-manifest/v1"
        or manifest["invocation_id"] != authorized.attempt_id
        or manifest["run_id"] != authorized.pipeline_run_id
        or manifest["stage"] != _WIRE_STAGE
        or manifest["slide_id"] != authorized.slide_id
        or _sequence(manifest["generation_ids"], "semantic generation_ids") != (generation_id,)
        or _sequence(manifest["generations"], "semantic generations") != ()
        or canonical_json_bytes(plain_json(manifest["object_outputs"]))
        != canonical_json_bytes(plain_json(outputs))
    ):
        raise BatchPhenotypingError("semantic Task-10 manifest differs")


def build_semantic_evidence_stage_codec(
    spec: StageAdapterSpec,
    *,
    owner_resolver: ProjectStageOwnerCapabilityResolver,
) -> ProjectStageCodec:
    """Compose candidate evidence around without retaining its evidence."""

    _evidence_spec(spec)
    if type(owner_resolver) is not ProjectStageOwnerCapabilityResolver:
        raise ProjectStageCodecError("semantic evidence requires exact owner resolver")
    binding = ProjectStageCodecBinding.create(
        adapter_spec_sha256=spec.spec_sha256,
        target_script_asset_id=(QuPathScriptAsset.EXPORT_SEMANTIC_CANDIDATE_EVIDENCE.value),
        wire_stage_id=_WIRE_STAGE,
        terminal_stage_id="semantic_evidence",
        request_codec_version=_CODEC_VERSION,
    )

    def project_request(
        context: BatchNodeExecutionContext,
        intent: ProjectStageTargetIntent,
    ) -> Mapping[str, object]:
        try:
            if len(intent.analysis_domain_bindings) != 1:
                raise BatchPhenotypingError("semantic evidence target is ambiguous")
            target = intent.analysis_domain_bindings[0]
            capability = _resolve_capability(
                owner_resolver,
                context,
                project_entry_id=intent.project_entry_id,
                binding=target,
                cls=SemanticEvidenceStageOwnerCapability,
            )
            assert isinstance(capability, SemanticEvidenceStageOwnerCapability)
            raw = capability.semantic_branch.raw_inventory
            if (
                target != capability.current.analysis_domain_bindings[0]
                or intent.sample_id != target.sample_id
                or intent.slide_id != target.slide_id
                or intent.resolved_sample is not capability.resolved_sample
                or intent.pipeline_run_id
                != capability.measurements.artifact_ref.owning_identity["pipeline_run_id"]
                or intent.saved_state_before_sha256 != raw.snapshot.saved_state_after_sha256
                or intent.prerequisite_artifact_ref_sha256s
                != capability.current.input_artifact_ref_sha256s
            ):
                raise BatchPhenotypingError("semantic evidence Task-10 target differs")
            return capability.request_body
        except Exception:
            owner_resolver._retire_for_attempt(context)
            raise

    def validate_result(value: object) -> BatchNodeExecutionResult:
        if type(value) is not ProjectStageTargetExecutionEvidence:
            raise BatchPhenotypingError(
                "semantic evidence requires exact Task-10 execution evidence"
            )
        evidence = value
        context = evidence.context
        try:
            if len(evidence.authorized.analysis_domain_bindings) != 1:
                raise BatchPhenotypingError("semantic evidence result is ambiguous")
            target = evidence.authorized.analysis_domain_bindings[0]
            capability = _resolve_capability(
                owner_resolver,
                context,
                project_entry_id=evidence.authorized.project_entry_id,
                binding=target,
                cls=SemanticEvidenceStageOwnerCapability,
            )
            assert isinstance(capability, SemanticEvidenceStageOwnerCapability)
            result = evidence.wire_stage_result
            if (
                context.adapter_spec != spec
                or evidence.authorized.request_codec_id != binding.target_script_asset_id
                or evidence.authorized.request_codec_version != _CODEC_VERSION
                or evidence.authorized.wire_stage_id != _WIRE_STAGE
                or result.stage != _WIRE_STAGE
            ):
                raise BatchPhenotypingError("semantic evidence execution identity differs")
            if result.status is not StageStatus.COMPLETED:
                return BatchNodeExecutionResult(context.authorization.authorization_sha256, ())
            retained = evidence.reopen_validation_inputs()
            if canonical_json_bytes(plain_json(retained.scientific_request_body)) != canonical_json_bytes(
                plain_json(capability.request_body)
            ):
                raise BatchPhenotypingError("semantic evidence retained request differs")
            outputs = _closed_mapping(
                result.outputs, _EVIDENCE_OUTPUT_FIELDS, "semantic evidence outputs"
            )
            raw = capability.semantic_branch.raw_inventory
            generation_id = raw.snapshot.tissue_generation_id
            _manifest(evidence, outputs=outputs, generation_id=generation_id)
            path = Path(str(outputs["path"])).resolve(strict=False)
            try:
                path.relative_to(Path(retained.operational_output_dir))
                byte_count = path.stat().st_size
            except (OSError, RuntimeError, ValueError) as exc:
                raise BatchPhenotypingError(
                    "semantic evidence output is outside its authenticated root"
                ) from exc
            content = read_bounded_file(
                path,
                expected_byte_count=byte_count,
                maximum_byte_count=512 * 1024 * 1024,
            )
            sha256 = hashlib.sha256(content).hexdigest()
            if (
                outputs["schema_version"] != "semantic-candidate-membership/v1"
                or outputs["artifact_sha256"] != sha256
                or outputs["candidate_count"] != len(raw.candidate_secondary_roi_ids)
                or outputs["cell_count"] != raw.snapshot.cell_count
                or outputs["coordinate_system"] != "global_level0"
                or evidence.observation.saved_state_disposition != "authenticated_unchanged"
                or evidence.observation.saved_state_before_sha256
                != raw.snapshot.saved_state_after_sha256
                or evidence.observation.saved_state_after_sha256
                != evidence.observation.saved_state_before_sha256
            ):
                raise BatchPhenotypingError("semantic evidence output or saved state differs")
            snapshots = validate_roi_semantic_evidence_response(
                raw_objects_path=Path(str(raw.artifact_ref.locator)),
                raw_export_sha256=raw.snapshot.raw_export_sha256,
                membership_artifact_path=path,
                membership_artifact_sha256=sha256,
                slide_id=target.slide_id,
                section_id=raw.snapshot.section_id,
                analysis_domain_id=target.analysis_domain_id,
                tissue_generation_id=generation_id,
                pipeline_run_id=evidence.authorized.pipeline_run_id,
                segmentation_population_id=raw.snapshot.segmentation_population_id,
                physical_calibration=capability.request_body["physical_calibration"],
                phenotype_view=capability.request_body["phenotype_view"],
            )
            return build_semantic_evidence_execution_result(
                context,
                capability.current,
                capability=capability,
                snapshots=snapshots,
                membership_path=path,
                membership_bytes=content,
                saved_state_before_sha256=(evidence.observation.saved_state_before_sha256),
                saved_state_after_sha256=(evidence.observation.saved_state_after_sha256),
                execution_exchange_sha256=evidence.execution_exchange_sha256,
            )
        finally:
            owner_resolver._retire_for_attempt(context)

    return ProjectStageCodec.for_stage_owner(
        binding=binding,
        spec=spec,
        request_projector=project_request,
        result_validator=validate_result,
    )


def _restage(result: StageResult, stage: str) -> StageResult:
    mapping = result.to_mapping()
    mapping["stage"] = stage
    return StageResult.from_mapping(mapping)


def _direct_outcome(
    context: BatchNodeExecutionContext,
    result: StageResult,
    output: object | None,
) -> BatchRawNodeOutcome:
    from mif_pipeline.orchestration.batch_stage_adapters import (
        compose_single_output_direct_stage_outcome,
    )

    return compose_single_output_direct_stage_outcome(context, result, output)


def _load_retry(
    context: BatchNodeExecutionContext,
    state_store: BatchStateStore,
) -> tuple[Mapping[str, object] | None, StageResult | None]:
    try:
        response = state_store.load_validated_interaction_response(context.authorization)
    except (BatchStateError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("semantic retry response is not authenticated") from exc
    authorization = context.authorization
    if response is None:
        if (
            authorization.retry_ordinal != 0
            or authorization.interaction_response_sha256 is not None
            or authorization.interaction_predecessor_terminal_record_sha256 is not None
        ):
            raise BatchPhenotypingError("semantic initial attempt has retry lineage")
        return None, None
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
    response = _closed_mapping(response, fields, "semantic interaction response")
    expected_validator = (
        _EVALUATION_VALIDATOR_ID
        if context.adapter_spec.node_kind == "semantic_evaluation"
        else _COUNT_VALIDATOR_ID if context.adapter_spec.node_kind == "final_count_cap" else None
    )
    try:
        request_id = batch_interaction_request_id(
            node_id=context.node_id,
            attempt_id=str(response["prior_attempt_id"]),
            attempt_authorization_sha256=str(response["prior_attempt_authorization_sha256"]),
            interaction_context_sha256=str(response["interaction_context_sha256"]),
        )
        predecessor = state_store.load_interaction_request(request_id)
    except (BatchStateError, KeyError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("semantic retry request is unavailable") from exc
    unsigned = {key: value for key, value in response.items() if key != "response_sha256"}
    if (
        authorization.retry_ordinal <= 0
        or response["schema_name"] != INTERACTION_RESPONSE_SCHEMA_NAME
        or response["schema_version"] != INTERACTION_RESPONSE_SCHEMA_VERSION
        or response["node_id"] != context.node_id
        or response["prior_terminal_record_sha256"]
        != authorization.interaction_predecessor_terminal_record_sha256
        or response["response_sha256"] != authorization.interaction_response_sha256
        or response["response_sha256"] != digest(unsigned)
        or response["interaction_request_id"] != request_id
        or response["response_validator_id"] != expected_validator
        or response["response_validator_version"] != "1"
        or not isinstance(response["response"], Mapping)
        or not isinstance(predecessor, StageResult)
        or predecessor.status is not StageStatus.NEEDS_INTERACTION
        or predecessor.resume_context is None
        or response["interaction_context_sha256"] != digest(predecessor.resume_context)
    ):
        raise BatchPhenotypingError("semantic retry lineage differs")
    return response, predecessor


def _current_definition_selection(context, state_store, sample):
    from ..batch_secondary_definitions import secondary_definition_selections
    return secondary_definition_selections(
        state_store.reopen_execution_snapshot(), state_store=state_store,
        participant_ids=(sample.sample_id,), node_id=context.node_id,
    ).get(sample.sample_id)


def _evaluation_inputs(
    context: BatchNodeExecutionContext,
    state_store: BatchStateStore,
    resolved_sample: ResolvedBatchSample,
) -> tuple[object, object, object, object]:
    if (
        type(context) is not BatchNodeExecutionContext
        or type(state_store) is not BatchStateStore
        or type(resolved_sample) is not ResolvedBatchSample
        or context.adapter_spec.node_kind != "semantic_evaluation"
        or context.adapter_spec.dispatch_mode is not DispatchMode.SINGLE
    ):
        raise BatchArtifactError("semantic evaluation requires exact direct owners")
    _stage_spec(
        context.adapter_spec,
        node_kind="semantic_evaluation",
        output_kind="semantic_evaluation_result",
        contract=semantic_evaluation_result_identity_contract(),
        dispatch=DispatchMode.SINGLE,
        required={
            "semantic_evidence_result": ArtifactMultiplicity.SINGLE,
            "semantic_raw_inventory": ArtifactMultiplicity.SINGLE,
            "semantic_profile": ArtifactMultiplicity.EXACT_SET,
        },
    )
    if context.adapter_spec.resource_kind != "scientific_compute":
        raise BatchArtifactError("semantic evaluation resource differs")
    profiles = tuple(item for item in context.inputs if item.artifact_kind == "semantic_profile")
    by_kind = {
        item.artifact_kind: item
        for item in context.inputs
        if item.artifact_kind != "semantic_profile"
    }
    if (
        set(by_kind)
        != {
            "semantic_evidence_result",
            "semantic_raw_inventory",
        }
        or not profiles
        or len(by_kind) + len(profiles) != len(context.inputs)
    ):
        raise BatchArtifactError("semantic evaluation input set differs")
    current = state_store.authenticate_current_execution_context(context)
    from ..batch_secondary_definitions import secondary_definition_selections
    secondary_selection = secondary_definition_selections(
        state_store.reopen_execution_snapshot(expected_head_sha256=current.state_head_sha256), state_store=state_store,
        participant_ids=(resolved_sample.sample_id,), node_id=context.node_id,
    ).get(resolved_sample.sample_id)
    owners = {
        kind: state_store.authenticate_available_artifact(ref) for kind, ref in by_kind.items()
    }
    profile_owners = tuple(
        state_store.authenticate_available_manifest_external_artifact(ref) for ref in profiles
    )
    evidence = _decode_semantic_evidence_for_evaluation(
        owners["semantic_evidence_result"],
        raw_inventory=owners["semantic_raw_inventory"],
        semantic_profiles=profile_owners,
        resolved_sample=resolved_sample,
        secondary_selection=secondary_selection,
    )
    raw = decode_committed_semantic_raw_inventory(owners["semantic_raw_inventory"])
    profile_authority, bindings, contents = _profile_owner(profile_owners, resolved_sample, secondary_selection)
    if (
        evidence.state_head_sha256 != current.state_head_sha256
        or evidence.active_plan_sha256 != current.active_plan_sha256
        or raw.state_head_sha256 != current.state_head_sha256
        or raw.active_plan_sha256 != current.active_plan_sha256
    ):
        raise BatchArtifactError("semantic evaluation owners are not current")
    return current, owners, evidence, (raw, profile_authority, bindings, contents, secondary_selection)


def _terminal_preliminary_retry(
    context: BatchNodeExecutionContext,
    state_store: BatchStateStore,
) -> tuple[
    tuple[SemanticRegionDecision, ...], str | None, Mapping[str, object] | None, object | None
]:
    """Recover the last criterion response consumed by the terminal-review attempt."""

    predecessor_sha256 = context.authorization.interaction_predecessor_terminal_record_sha256
    if predecessor_sha256 is None:
        raise BatchPhenotypingError("terminal semantic review lacks its predecessor")
    try:
        terminal_attempt = state_store.load_attempt(predecessor_sha256)
        terminal_authorization = state_store.load_authorization(
            terminal_attempt.authorization_sha256
        )
        prior_response = state_store.load_validated_interaction_response(terminal_authorization)
    except (BatchStateError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("terminal semantic review history is unavailable") from exc
    if terminal_attempt.node_id != context.node_id:
        raise BatchPhenotypingError("terminal semantic review history is foreign")
    if prior_response is None:
        if terminal_authorization.retry_ordinal != 0:
            raise BatchPhenotypingError("terminal semantic review has incomplete criterion history")
        return (), None, None, None
    try:
        prior_request = state_store.load_interaction_request(
            str(prior_response["interaction_request_id"])
        )
    except (BatchStateError, KeyError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("terminal semantic criterion request is unavailable") from exc
    if (
        prior_request.interaction_reason is not InteractionReason.SEMANTIC_REGION_DECISION_REQUIRED
        or prior_response.get("response_validator_id") != _EVALUATION_VALIDATOR_ID
        or prior_response.get("response_validator_version") != "1"
        or prior_response.get("node_id") != context.node_id
        or not isinstance(prior_response.get("response"), Mapping)
    ):
        raise BatchPhenotypingError("terminal semantic criterion history differs")
    service = resolve_resume_envelope(prior_request.resume_context or {}).service_context
    try:
        decisions = tuple(
            SemanticRegionDecision.from_mapping(item)
            for item in service.get("semantic_region_decisions", ())
        )
    except (TypeError, ValueError) as exc:
        raise BatchPhenotypingError("terminal semantic criterion history is invalid") from exc
    return (
        decisions,
        str(prior_response["action"]),
        service,
        prior_response["response"],
    )


def execute_semantic_evaluation_stage(
    context: BatchNodeExecutionContext,
    *,
    state_store: BatchStateStore,
    resolved_sample: ResolvedBatchSample,
) -> BatchRawNodeOutcome:
    """Evaluate criteria, terminal review, and result publication directly."""

    current, owners, evidence, profile_values = _evaluation_inputs(
        context, state_store, resolved_sample
    )
    raw, profile_authority, profile_bindings, profile_bytes, secondary_selection = profile_values
    from ..batch_secondary_definitions import resolve_secondary_structures
    structures = resolve_secondary_structures(resolved_sample, secondary_selection)
    response, predecessor = _load_retry(context, state_store)
    decisions: tuple[SemanticRegionDecision, ...] = ()
    action = None
    offered = None
    normalized_response = None
    terminal_retry = False
    if response is not None and predecessor is not None:
        service = resolve_resume_envelope(predecessor.resume_context or {}).service_context
        raw_decisions = service.get("semantic_region_decisions", ())
        try:
            decisions = tuple(SemanticRegionDecision.from_mapping(item) for item in raw_decisions)
        except (TypeError, ValueError) as exc:
            raise BatchPhenotypingError("semantic decision predecessor is invalid") from exc
        if predecessor.interaction_reason is InteractionReason.SEMANTIC_REGION_DECISION_REQUIRED:
            action = str(response["action"])
            offered = service
            normalized_response = response["response"]
        elif predecessor.interaction_reason is InteractionReason.ROI_REVIEW_REQUIRED:
            terminal_retry = True
            decisions, action, offered, normalized_response = _terminal_preliminary_retry(
                context, state_store
            )
        else:
            raise BatchPhenotypingError("semantic evaluation retry reason differs")
    binding = raw.artifact_ref.scope.analysis_domain_bindings[0]
    pipeline_run_id = evidence.pipeline_run_id
    raw_inventory = validate_roi_semantic_raw_inventory(
        Path(str(raw.artifact_ref.locator)),
        raw_export_sha256=raw.snapshot.raw_export_sha256,
        slide_id=binding.slide_id,
        section_id=raw.snapshot.section_id,
        analysis_domain_id=binding.analysis_domain_id,
        tissue_generation_id=raw.snapshot.tissue_generation_id,
        pipeline_run_id=pipeline_run_id,
    )
    semantic_config_sha256 = digest(resolved_sample.config.semantic.to_mapping())
    evaluated = evaluate_roi_semantic_result_set(
        profile_bindings=profile_bindings,
        profile_bytes=profile_bytes,
        expected_validation_domain_id=current.validation_domain_id,
        configured_structure_ids=tuple(
            item.structure_id for item in structures
        ),
        evidence=evidence.snapshots,
        decisions=decisions,
        raw_inventory=raw_inventory,
        slide_id=binding.slide_id,
        section_id=raw.snapshot.section_id,
        analysis_domain_id=binding.analysis_domain_id,
        tissue_generation_id=raw.snapshot.tissue_generation_id,
        pipeline_run_id=pipeline_run_id,
        review_status="needs_review",
        semantic_config_sha256=semantic_config_sha256,
        action=action,
        offered_context=offered,
        response=normalized_response,
    )
    if isinstance(evaluated, StageResult):
        if evaluated.resume_context.get("schema_version") == "semantic-region-interaction/v2":
            structure_name = next(item.display_name for item in structures
                                  if item.structure_id == evaluated.resume_context["structure_id"])
            evaluated = replace(evaluated, resume_context={
                **evaluated.resume_context, "structure_display_name": structure_name,
            })
        return _direct_outcome(context, _restage(evaluated, "semantic_evaluation"), None)
    preliminary = {
        **evaluated,
        "pipeline_run_id": pipeline_run_id,
        "semantic_config_sha256": semantic_config_sha256,
    }
    authorized_action = None
    unified = all(item.get("schema_version") == "semantic-result/v3" for item in preliminary["semantic_results"])
    if unified:
        resolved = "accepted"  # Review is complete; individual rows retain accept/reject.
        final = {**preliminary, "review_status": resolved}
    else:
        if terminal_retry:
            assert response is not None and predecessor is not None
            authorized_action = validate_roi_terminal_semantic_review_response(
                predecessor,
                action=str(response["action"]),
                response=response["response"],
            )
        resolved = resolve_roi_terminal_semantic_review(
            review_policy=resolved_sample.config.semantic.roi_review_policy,
            preliminary_output=preliminary,
            authorized_action=authorized_action,
            stage="semantic_evaluation",
            slide_id=binding.slide_id,
            section_id=raw.snapshot.section_id,
            generation_id=raw.snapshot.tissue_generation_id,
        )
        if isinstance(resolved, StageResult):
            return _direct_outcome(context, resolved, None)
        final_decisions = tuple(
            SemanticRegionDecision.from_mapping(item)
            for item in preliminary["semantic_region_decisions"]
        )
        final = evaluate_roi_semantic_result_set(
            profile_bindings=profile_bindings,
            profile_bytes=profile_bytes,
            expected_validation_domain_id=current.validation_domain_id,
            configured_structure_ids=tuple(
                item.structure_id for item in structures
            ),
            evidence=evidence.snapshots,
            decisions=final_decisions,
            raw_inventory=raw_inventory,
            slide_id=binding.slide_id,
            section_id=raw.snapshot.section_id,
            analysis_domain_id=binding.analysis_domain_id,
            tissue_generation_id=raw.snapshot.tissue_generation_id,
            pipeline_run_id=pipeline_run_id,
            review_status=resolved,
            semantic_config_sha256=semantic_config_sha256,
        )
        if not isinstance(final, Mapping):
            raise BatchPhenotypingError("final semantic evaluation remained interactive")
    artifact = publish_roi_semantic_result_artifact(
        output_dir=operational_sample_output_root(resolved_sample, state_store.layout.root) / "analysis",
        pipeline_run_id=pipeline_run_id,
        analysis_domain_id=binding.analysis_domain_id,
        result_rows=final["semantic_results"],
        result_set_sha256=str(final["semantic_result_set_sha256"]),
    )
    execution = build_semantic_evaluation_execution_result(
        context,
        current,
        evidence=evidence,
        raw=raw,
        profile_authorities=profile_authority,
        resolved_sample=resolved_sample,
        evaluated={
            **final,
            "pipeline_run_id": pipeline_run_id,
            "semantic_config_sha256": semantic_config_sha256,
        },
        result_artifact=artifact,
        review_action=(None if authorized_action is None else str(authorized_action["action"])),
    )
    output = execution.outputs[0]
    return _direct_outcome(
        context,
        StageResult.completed(
            stage="semantic_evaluation",
            slide_id=binding.slide_id,
            generation_id=raw.snapshot.tissue_generation_id,
        ),
        output,
    )


def _manual_authority(
    response: Mapping[str, object] | None,
    predecessor: StageResult | None,
) -> tuple[dict[str, ManualSelectionLineage], tuple[ManualSelectionEvidence, ...]]:
    if response is None:
        return {}, ()
    if (
        predecessor is None
        or predecessor.interaction_reason
        is not InteractionReason.SECONDARY_STRUCTURE_SELECTION_REQUIRED
    ):
        raise BatchPhenotypingError("final count retry reason differs")
    raw = response.get("response")
    if not isinstance(raw, Mapping) or set(raw) != {
        "manual_selection_lineages",
        "manual_selection_evidence",
    }:
        raise BatchPhenotypingError("final count accumulated response differs")
    lineage_values = raw["manual_selection_lineages"]
    evidence_values = raw["manual_selection_evidence"]
    if not isinstance(lineage_values, Mapping):
        raise BatchPhenotypingError("manual selection lineage inventory is invalid")
    try:
        lineages = {
            str(key): ManualSelectionLineage(
                **{
                    **dict(value),
                    "algorithm": SelectionAlgorithm(value["algorithm"]),
                    "selected_content_keys": tuple(value["selected_content_keys"]),
                }
            )
            for key, value in lineage_values.items()
            if isinstance(value, Mapping)
        }
        evidence = tuple(
            ManualSelectionEvidence.from_mapping(item)
            for item in _sequence(evidence_values, "manual selection evidence")
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BatchPhenotypingError("manual selection authority is invalid") from exc
    if (
        set(lineages) != set(lineage_values)
        or tuple(item.structure_id for item in evidence) != tuple(sorted(lineages))
        or any(
            plain_json(asdict(lineages[key])) != plain_json(lineage_values[key]) for key in lineages
        )
        or any(
            plain_json(item.selection_lineage) != plain_json(asdict(lineages[item.structure_id]))
            for item in evidence
        )
    ):
        raise BatchPhenotypingError("manual selection lineage inventory is invalid")
    return lineages, evidence


def execute_final_count_cap_stage(
    context: BatchNodeExecutionContext,
    *,
    state_store: BatchStateStore,
    resolved_sample: ResolvedBatchSample,
) -> BatchRawNodeOutcome:
    """Resolve automatic or sequential state-authorized manual count caps."""

    if (
        type(context) is not BatchNodeExecutionContext
        or type(state_store) is not BatchStateStore
        or type(resolved_sample) is not ResolvedBatchSample
        or context.adapter_spec.node_kind != "final_count_cap"
        or context.adapter_spec.dispatch_mode is not DispatchMode.SINGLE
    ):
        raise BatchArtifactError("final count/cap requires exact direct owners")
    _stage_spec(
        context.adapter_spec,
        node_kind="final_count_cap",
        output_kind="final_count_cap_result",
        contract=final_count_cap_result_identity_contract(),
        dispatch=DispatchMode.SINGLE,
        required={
            "semantic_evaluation_result": ArtifactMultiplicity.SINGLE,
            "semantic_raw_inventory": ArtifactMultiplicity.SINGLE,
            "accepted_analysis_domain": ArtifactMultiplicity.SINGLE,
        },
    )
    if context.adapter_spec.resource_kind != "scientific_compute":
        raise BatchArtifactError("final count/cap resource differs")
    by_kind = {item.artifact_kind: item for item in context.inputs}
    if set(by_kind) != {
        "semantic_evaluation_result",
        "semantic_raw_inventory",
        "accepted_analysis_domain",
    } or len(by_kind) != len(context.inputs):
        raise BatchArtifactError("final count/cap input set differs")
    current = state_store.authenticate_current_execution_context(context)
    owners = {
        kind: state_store.authenticate_available_artifact(ref) for kind, ref in by_kind.items()
    }
    evaluation = _decode_semantic_evaluation_for_count(
        owners["semantic_evaluation_result"],
        raw_inventory=owners["semantic_raw_inventory"],
        resolved_sample=resolved_sample,
    )
    raw, _domain, domain_ref, values, contexts = _count_inputs(
        evaluation,
        owners["semantic_raw_inventory"],
        owners["accepted_analysis_domain"],
        resolved_sample,
        secondary_selection=_current_definition_selection(context, state_store, resolved_sample),
    )
    if (
        evaluation.state_head_sha256 != current.state_head_sha256
        or evaluation.active_plan_sha256 != current.active_plan_sha256
    ):
        raise BatchArtifactError("final count/cap owners are not current")
    response, predecessor = _load_retry(context, state_store)
    lineages, manual_evidence = _manual_authority(response, predecessor)
    resolution = resolve_roi_semantic_count(
        domain=domain_ref,
        result_rows=evaluation.result_rows,
        raw_inventory=values["inventory"],
        policies=values["policies"],
        selection_contexts=contexts,
        manual_selections=lineages,
    )
    requirement = resolution["manual_selection_requirement"]
    binding = raw.artifact_ref.scope.analysis_domain_bindings[0]
    if requirement is not None:
        interaction = build_roi_semantic_manual_cap_interaction(
            requirement=requirement,
            prior_lineages=lineages,
            prior_evidence=manual_evidence,
            output_dir=operational_sample_output_root(resolved_sample, state_store.layout.root) / "analysis",
            slide_id=binding.slide_id,
            section_id=raw.snapshot.section_id,
            analysis_domain=domain_ref,
            analysis_domain_revision=binding.analysis_domain_revision,
            analysis_domain_revision_sha256=binding.analysis_domain_revision_sha256,
            pipeline_run_id=evaluation.pipeline_run_id,
            config_sha256=resolved_sample.resolved_config_sha256,
            semantic_result_set_sha256=evaluation.result_set_sha256,
            semantic_count_state_descriptor={
                "schema_version": "mif-batch-semantic-count-state/v1",
                "semantic_evaluation_ref_sha256": evaluation.artifact_ref.artifact_ref_sha256,
                "raw_inventory_ref_sha256": raw.artifact_ref.artifact_ref_sha256,
                "accepted_domain_ref_sha256": owners[
                    "accepted_analysis_domain"
                ].artifact_ref.artifact_ref_sha256,
            },
        )
        return _direct_outcome(context, _restage(interaction, "final_count_cap"), None)
    execution = build_final_count_cap_execution_result(
        context,
        current,
        evaluation=evaluation,
        raw=raw,
        accepted_domain=owners["accepted_analysis_domain"],
        resolved_sample=resolved_sample,
        resolution=resolution,
        manual_lineages=lineages,
        manual_evidence=manual_evidence,
    )
    output = execution.outputs[0]
    return _direct_outcome(
        context,
        StageResult.completed(
            stage="final_count_cap",
            slide_id=binding.slide_id,
            generation_id=raw.snapshot.tissue_generation_id,
        ),
        output,
    )


def build_semantic_finalization_stage_codec(
    spec: StageAdapterSpec,
    *,
    owner_resolver: ProjectStageOwnerCapabilityResolver,
) -> ProjectStageCodec:
    """Compose the terminal semantic mutation around the committer."""

    _stage_spec(
        spec,
        node_kind="semantic_finalization",
        output_kind="semantic_finalization_result",
        contract=semantic_finalization_result_identity_contract(),
        dispatch=DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE,
        required={
            "semantic_evaluation_result": ArtifactMultiplicity.SINGLE,
            "final_count_cap_result": ArtifactMultiplicity.SINGLE,
            "semantic_raw_inventory": ArtifactMultiplicity.SINGLE,
            "analysis_frame": ArtifactMultiplicity.SINGLE,
            "accepted_analysis_domain": ArtifactMultiplicity.SINGLE,
            "measurements_result": ArtifactMultiplicity.SINGLE,
        },
    )
    if spec.resource_kind != "qupath_image_write":
        raise ProjectStageCodecError("semantic finalization resource differs")
    if type(owner_resolver) is not ProjectStageOwnerCapabilityResolver:
        raise ProjectStageCodecError("semantic finalization requires exact owner resolver")
    binding = ProjectStageCodecBinding.create(
        adapter_spec_sha256=spec.spec_sha256,
        target_script_asset_id=QuPathScriptAsset.FINALIZE_SEMANTIC_ROI_RESULTS.value,
        wire_stage_id=_WIRE_STAGE,
        terminal_stage_id="semantic_finalization",
        request_codec_version=_CODEC_VERSION,
    )

    def project_request(
        context: BatchNodeExecutionContext,
        intent: ProjectStageTargetIntent,
    ) -> Mapping[str, object]:
        try:
            if len(intent.analysis_domain_bindings) != 1:
                raise BatchPhenotypingError("semantic finalization target is ambiguous")
            target = intent.analysis_domain_bindings[0]
            capability = _resolve_capability(
                owner_resolver,
                context,
                project_entry_id=intent.project_entry_id,
                binding=target,
                cls=SemanticFinalizationStageOwnerCapability,
            )
            assert isinstance(capability, SemanticFinalizationStageOwnerCapability)
            raw = capability.raw_inventory
            if (
                target != capability.current.analysis_domain_bindings[0]
                or intent.sample_id != target.sample_id
                or intent.slide_id != target.slide_id
                or intent.resolved_sample is not capability.resolved_sample
                or intent.pipeline_run_id != capability.semantic_evaluation.pipeline_run_id
                or intent.saved_state_before_sha256 != raw.snapshot.saved_state_after_sha256
                or intent.prerequisite_artifact_ref_sha256s
                != capability.current.input_artifact_ref_sha256s
            ):
                raise BatchPhenotypingError("semantic finalization Task-10 target differs")
            return capability.request_body
        except Exception:
            owner_resolver._retire_for_attempt(context)
            raise

    def validate_result(value: object) -> BatchNodeExecutionResult:
        if type(value) is not ProjectStageTargetExecutionEvidence:
            raise BatchPhenotypingError(
                "semantic finalization requires exact Task-10 execution evidence"
            )
        evidence = value
        context = evidence.context
        try:
            if len(evidence.authorized.analysis_domain_bindings) != 1:
                raise BatchPhenotypingError("semantic finalization result is ambiguous")
            target = evidence.authorized.analysis_domain_bindings[0]
            capability = _resolve_capability(
                owner_resolver,
                context,
                project_entry_id=evidence.authorized.project_entry_id,
                binding=target,
                cls=SemanticFinalizationStageOwnerCapability,
            )
            assert isinstance(capability, SemanticFinalizationStageOwnerCapability)
            result = evidence.wire_stage_result
            if (
                context.adapter_spec != spec
                or evidence.authorized.request_codec_id != binding.target_script_asset_id
                or evidence.authorized.request_codec_version != _CODEC_VERSION
                or evidence.authorized.wire_stage_id != _WIRE_STAGE
                or result.stage != _WIRE_STAGE
            ):
                raise BatchPhenotypingError("semantic finalization execution differs")
            if result.status is not StageStatus.COMPLETED:
                return BatchNodeExecutionResult(context.authorization.authorization_sha256, ())
            retained = evidence.reopen_validation_inputs()
            if canonical_json_bytes(plain_json(retained.scientific_request_body)) != canonical_json_bytes(
                plain_json(capability.request_body)
            ):
                raise BatchPhenotypingError("semantic finalization retained request differs")
            outputs = _closed_mapping(
                result.outputs, _FINAL_OUTPUT_FIELDS, "semantic finalization outputs"
            )
            raw = capability.raw_inventory
            _manifest(
                evidence,
                outputs=outputs,
                generation_id=raw.snapshot.tissue_generation_id,
            )
            if (
                evidence.observation.saved_state_disposition != "authenticated_changed"
                or evidence.observation.saved_state_before_sha256
                != raw.snapshot.saved_state_after_sha256
                or evidence.observation.saved_state_after_sha256
                == evidence.observation.saved_state_before_sha256
                or outputs["section_id"] != raw.snapshot.section_id
                or outputs["coordinate_system"] != "global_level0"
            ):
                raise BatchPhenotypingError("semantic finalization saved state or scope differs")
            evaluation = capability.semantic_evaluation
            count = capability.final_count_cap
            receipt = validate_roi_semantic_finalization_response(
                outputs,
                result_rows=evaluation.result_rows,
                result_set_sha256=evaluation.result_set_sha256,
                result_artifact=evaluation.result_artifact,
                result_artifact_bytes=capability.result_artifact_bytes,
                count_decision=count.count_decision,
                final_object_dispositions=count.dispositions,
                active_secondary_roi_ids=count.active_secondary_roi_ids,
                analysis_domain_id=target.analysis_domain_id,
                section_id=raw.snapshot.section_id,
                tissue_generation_id=raw.snapshot.tissue_generation_id,
                pipeline_run_id=evaluation.pipeline_run_id,
                expected_review_status=evaluation.review_status,
                expected_review_action=evaluation.review_action,
            )
            frame = validate_analysis_frame_artifact_ref(capability.analysis_frame.artifact_ref)
            finalized = FinalizedSecondaryDomainEvidence.create(
                analysis_domain_id=target.analysis_domain_id,
                analysis_domain_revision_sha256=target.analysis_domain_revision_sha256,
                domain_set_sha256=target.domain_set_sha256,
                spatial_frame_sha256=frame.spatial_frame.spatial_frame_sha256,
                semantic_result_artifact=evaluation.result_artifact,
                count_decision=count.count_decision_mapping,
                decision_sha256=str(count.count_decision_mapping["decision_sha256"]),
                dispositions=count.dispositions,
                manual_selections=count.manual_evidence,
                finalization_receipt=receipt,
                domain_status=count.domain_status,
            )
            return build_semantic_finalization_execution_result(
                context,
                capability.current,
                evidence=finalized,
                component_descriptor=outputs["component_descriptor"],
                population_id=raw.snapshot.segmentation_population_id,
                runtime_versions=raw.artifact_ref.runtime_versions,
            )
        finally:
            owner_resolver._retire_for_attempt(context)

    return ProjectStageCodec.for_stage_owner(
        binding=binding,
        spec=spec,
        request_projector=project_request,
        result_validator=validate_result,
    )


def _rejected(target: BatchInteractionTarget, code: str) -> BatchTargetResponseValidation:
    return BatchTargetResponseValidation(
        target_id=target.target_id,
        node_id=target.node_id,
        status="rejected",
        response=None,
        authorization_request=None,
        response_validator_id=None,
        response_validator_version=None,
        rejection_code=code,
    )


def validate_semantic_evaluation_target_response(
    target: BatchInteractionTarget,
    action: str,
    response: Mapping[str, object],
    *,
    authorization_request: BatchAttemptAuthorizationRequest,
) -> BatchTargetResponseValidation:
    """Admit only the generic semantic or Gate-A terminal response."""

    if not isinstance(target, BatchInteractionTarget) or not isinstance(
        authorization_request, BatchAttemptAuthorizationRequest
    ):
        raise TypeError("semantic evaluation response requires typed batch owners")
    try:
        result = target.stage_result
        if result.interaction_reason is InteractionReason.ROI_REVIEW_REQUIRED:
            validated = validate_roi_terminal_semantic_review_response(
                result, action=action, response=response
            )
            canonical = plain_json(validated["response"])
        elif result.interaction_reason is InteractionReason.SEMANTIC_REGION_DECISION_REQUIRED:
            validated = validate_interaction_response(result, action, response)
            canonical = plain_json(validated["response"])
        else:
            raise ValueError("foreign semantic interaction")
        assert isinstance(canonical, Mapping)
    except (TypeError, ValueError, KeyError):
        return _rejected(target, "semantic_evaluation_response_invalid")
    return BatchTargetResponseValidation(
        target_id=target.target_id,
        node_id=target.node_id,
        status="accepted",
        response=canonical,
        authorization_request=authorization_request,
        response_validator_id=_EVALUATION_VALIDATOR_ID,
        response_validator_version="1",
        rejection_code=None,
    )


def validate_final_count_cap_target_response(
    target: BatchInteractionTarget,
    action: str,
    response: Mapping[str, object],
    *,
    authorization_request: BatchAttemptAuthorizationRequest,
) -> BatchTargetResponseValidation:
    """Persist Gate-A's exact accumulated typed cap authority for the retry."""

    if not isinstance(target, BatchInteractionTarget) or not isinstance(
        authorization_request, BatchAttemptAuthorizationRequest
    ):
        raise TypeError("final count response requires typed batch owners")
    try:
        lineages, evidence = validate_roi_semantic_manual_cap_response(
            target.stage_result, action=action, response=response
        )
        canonical = {
            "manual_selection_lineages": {
                key: plain_json(asdict(lineages[key])) for key in sorted(lineages)
            },
            "manual_selection_evidence": [item.to_mapping() for item in evidence],
        }
    except (TypeError, ValueError, KeyError):
        return _rejected(target, "final_count_cap_response_invalid")
    return BatchTargetResponseValidation(
        target_id=target.target_id,
        node_id=target.node_id,
        status="accepted",
        response=canonical,
        authorization_request=authorization_request,
        response_validator_id=_COUNT_VALIDATOR_ID,
        response_validator_version="1",
        rejection_code=None,
    )


__all__ = [
    "build_semantic_evidence_stage_codec",
    "build_semantic_finalization_stage_codec",
    "execute_final_count_cap_stage",
    "execute_semantic_evaluation_stage",
    "validate_final_count_cap_target_response",
    "validate_semantic_evaluation_target_response",
]
