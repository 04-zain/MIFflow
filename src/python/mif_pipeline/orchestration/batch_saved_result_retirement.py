"""Retire exact saved scientific selections before revised work.

Existing applied-result references describe the section, population and immutable
descriptor or generated annotation owner. They remain historical evidence in the new task, never available
scientific inputs. Native saving, partial failures and retries use project_stage.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import re

from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from .batch_artifacts import (
    ArtifactEquivalenceContract, ArtifactIdentityContract, ArtifactOrigin,
    ArtifactScope, BatchArtifactError, BatchArtifactRef, BatchNodeExecutionResult,
    DispatchMode, StageAdapterSpec,
)

KIND = "saved_result_retirement"
OUTPUT = "saved_result_retirement_result"
DISPLAY_KINDS = frozenset({"marker_rule_application", "supervised_phenotype_inference", "clustering_label_import"})


DERIVED_KINDS = frozenset({"segmentation", "primary_classifier_inference", "secondary_classifier_inference"})


def retirement_kinds(root_kind):
    return DISPLAY_KINDS | DERIVED_KINDS if root_kind in {
        "secondary_structure_definition",
        "inspection", "reference_unmixing", "tissue_setup", "tissue_resolution", "tissue_candidate", "analysis_domain_selection", "registration",
        "primary_classifier_training_plan_review", "secondary_classifier_training_plan_review",
        "supervised_phenotype_training_plan_review",
    } else DISPLAY_KINDS


def retirement_contracts():
    from .batch_phenotyping.marker_application_records import marker_section_application_identity_contract
    from .batch_phenotyping.supervised_records import supervised_phenotype_result_identity_contract
    from .batch_phenotyping.application_codec import cluster_label_import_result_identity_contract
    from .batch_segmentation_artifacts import segmentation_result_identity_contract
    from .batch_classifier_inference_contracts import (primary_classifier_inference_result_identity_contract, secondary_classifier_inference_result_identity_contract)
    return {contract.artifact_kind: contract for contract in (
        marker_section_application_identity_contract(), supervised_phenotype_result_identity_contract(),
        cluster_label_import_result_identity_contract(), segmentation_result_identity_contract(),
        primary_classifier_inference_result_identity_contract(), secondary_classifier_inference_result_identity_contract())}


@dataclass(frozen=True, slots=True)
class SavedResultRetirement:
    predecessor_node_id: str
    predecessor_plan_sha256: str
    sources: tuple[BatchArtifactRef, ...]

    def __post_init__(self):
        if (type(self.predecessor_node_id) is not str
                or re.fullmatch(r"node-[0-9a-f]{48}", self.predecessor_node_id) is None
                or type(self.predecessor_plan_sha256) is not str
                or re.fullmatch(r"[0-9a-f]{64}", self.predecessor_plan_sha256) is None):
            raise BatchArtifactError("Saved result retirement requires its exact predecessor task and plan.")
        if (type(self.sources) is not tuple or not self.sources
                or any(type(ref) is not BatchArtifactRef or ref.origin is not ArtifactOrigin.INTERNAL_NODE
                       for ref in self.sources)):
            raise BatchArtifactError("Saved result retirement requires existing typed saved results.")
        if self.sources != tuple(sorted(self.sources, key=lambda ref: ref.artifact_ref_sha256)) or len({
            ref.artifact_ref_sha256 for ref in self.sources}) != len(self.sources):
            raise BatchArtifactError("Saved result retirement sources must be unique and sorted.")
        contracts = retirement_contracts()
        for ref in self.sources:
            if ref.identity_contract != contracts.get(ref.artifact_kind) or not ref.scope.project_entry_id:
                raise BatchArtifactError("Saved result retirement source is not a saved scientific result.")

    def to_mapping(self):
        return {"predecessor_node_id": self.predecessor_node_id,
                "predecessor_plan_sha256": self.predecessor_plan_sha256,
                "sources": [ref.to_mapping() for ref in self.sources]}

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping) or set(value) != {"predecessor_node_id", "predecessor_plan_sha256", "sources"}:
            raise BatchArtifactError("Saved result retirement fields differ from the current contract.")
        if not isinstance(value["sources"], (list, tuple)):
            raise BatchArtifactError("Saved result retirement sources must be a sequence.")
        contracts = retirement_contracts()
        try:
            sources = tuple(BatchArtifactRef.from_mapping(ref, identity_contract=contracts[ref["artifact_kind"]])
                            for ref in value["sources"])
        except (KeyError, TypeError) as exc:
            raise BatchArtifactError("Saved result retirement source has no saved-result owner.") from exc
        return cls(value["predecessor_node_id"], value["predecessor_plan_sha256"], sources)


def retirement_result_identity_contract():
    return ArtifactIdentityContract(
        contract_id="mif-saved-result-retirement-identity/v1", artifact_kind=OUTPUT,
        schema_name="roi-generation-manifest", schema_version=1,
        required_fields=tuple(sorted(("node_id", "attempt_id", "predecessor_node_id",
            "retired_artifact_ref_sha256s", "saved_state_before_sha256", "saved_state_after_sha256",
            "saved_state_disposition", "validated_exchange_sha256"))), optional_fields=(),
        required_runtime_names=("mif_pipeline", "qupath"))


def build_saved_result_retirement_spec():
    from .batch_dag import ResourceKind
    contract = retirement_result_identity_contract()
    return StageAdapterSpec(
        node_kind=KIND, node_version="1", required_inputs=(), optional_inputs=(),
        declared_outputs=(OUTPUT,), output_identity_contracts=(contract,),
        compatibility_signature_id="mif-saved-result-retirement-compatibility/v1",
        equivalence_contract=ArtifactEquivalenceContract(
            contract_id="mif-saved-result-retirement-equivalence/v1", byte_deterministic=False,
            canonical_identity_paths=((OUTPUT, "predecessor_node_id"),),
            stable_inventory_paths=((OUTPUT, "retired_artifact_ref_sha256s"),),
            scientific_value_paths=((OUTPUT, "saved_state_after_sha256"),),
            qc_evidence_paths=((OUTPUT, "validated_exchange_sha256"),)),
        resource_kind=ResourceKind.QUPATH_IMAGE_WRITE.value,
        dispatch_mode=DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE)


def revision_predecessor(node):
    for field in ("segmentation_settings_revision", "measurement_settings_revision", "settled_review_revision", "cell_correction"):
        revision = getattr(node, field, None)
        if revision is not None:
            return revision.predecessor_node_id
    return None


def _binding_for_context(context, state_store):
    current = state_store.authenticate_current_execution_context(context)
    saved = state_store._load_plan_snapshot(current.active_plan_sha256).node_by_id[context.node_id]
    binding = SavedResultRetirement.from_mapping(saved["saved_result_retirement"])
    if (current.node_kind != KIND or len(current.project_entry_ids) != 1
            or {ref.scope.project_entry_id for ref in binding.sources} != set(current.project_entry_ids)):
        raise BatchArtifactError("Saved result retirement differs from its current entry task.")
    return current, binding


def _project_body(current, binding, target):
    if current.project_entry_ids != (target.project_entry_id,) or current.participant_ids != (target.sample_id,):
        raise BatchArtifactError("Saved result retirement target differs from the current task.")
    return {"schema_version": "roi-workflow-invocation/v1", "stage": KIND,
            "run_id": target.pipeline_run_id, "slide_id": target.slide_id,
            "sample_id": target.sample_id, "retirement": binding.to_mapping(),
            "source_ref_sha256s": [ref.artifact_ref_sha256 for ref in binding.sources]}


def validate_saved_result_retirement(evidence, *, state_store, acceptance_context=None):
    """Check the saved removal of result selections and return its accepted record.

    Ordinary completion uses the native attempt. A completed-result Retry uses
    the newly authorized attempt for publication and the original native request,
    saved-image observations and exchange for its evidence. The caller reopens
    that exact failed attempt and checks the current image before recovery.
    """
    from mif_pipeline.pipeline_core.stage_result import StageStatus

    context = evidence.context if acceptance_context is None else acceptance_context
    target = evidence.authorized
    if acceptance_context is not None and (
            context.authorization.failed_retry is None
            or context.node_id != evidence.context.node_id
            or context.inputs != evidence.context.inputs
            or context.authorization.retry_ordinal
            != evidence.context.authorization.retry_ordinal + 1):
        raise BatchArtifactError("Saved-result recovery differs from its failed native attempt.")
    current, binding = _binding_for_context(context, state_store)
    validation = evidence.reopen_validation_inputs()
    expected = canonical_json_bytes(_project_body(current, binding, target))
    if (hashlib.sha256(expected).hexdigest() != validation.scientific_request_body_sha256
            or len(expected) != validation.scientific_request_body_byte_count):
        raise BatchArtifactError("Saved result retirement request changed from its admitted task.")
    result = evidence.wire_stage_result
    if result.status is not StageStatus.COMPLETED:
        return BatchNodeExecutionResult(context.authorization.authorization_sha256, ())
    retired = tuple(ref.artifact_ref_sha256 for ref in binding.sources)
    outputs = result.outputs
    if (not isinstance(outputs, Mapping) or set(outputs) != {"retired_artifact_ref_sha256s"}
            or tuple(outputs["retired_artifact_ref_sha256s"]) != retired
            or evidence.observation.saved_state_disposition == "unattestable"):
        raise BatchArtifactError("Saved result retirement does not cover its exact saved results.")
    manifest = evidence.generation_manifest
    if (not isinstance(manifest, Mapping) or manifest.get("object_outputs") != outputs
            or tuple(manifest.get("generations", ())) != ()
            or tuple(manifest.get("generation_ids", ())) != (result.generation_id,)):
        raise BatchArtifactError("Saved result retirement manifest differs from its result.")
    observation = evidence.observation
    identity = {"node_id": context.node_id, "attempt_id": context.authorization.attempt_id,
        "predecessor_node_id": binding.predecessor_node_id,
        "retired_artifact_ref_sha256s": list(retired),
        "saved_state_before_sha256": observation.saved_state_before_sha256,
        "saved_state_after_sha256": observation.saved_state_after_sha256,
        "saved_state_disposition": observation.saved_state_disposition,
        "validated_exchange_sha256": evidence.validated_exchange_sha256}
    contract = retirement_result_identity_contract()
    source = binding.sources[0]
    artifact = BatchArtifactRef(
        artifact_id="retired-result-" + context.node_id[5:], artifact_kind=OUTPUT,
        schema_name=contract.schema_name, schema_version=contract.schema_version,
        origin=ArtifactOrigin.INTERNAL_NODE, producer_node_id=context.node_id,
        producer_attempt_id=context.authorization.attempt_id,
        producer_attempt_authorization_sha256=context.authorization.authorization_sha256,
        external_declaration_sha256=None, external_validation_receipt_sha256=None,
        locator=None, sha256=None, byte_count=None,
        scope=ArtifactScope(current.validation_domain_id, target.sample_id, target.slide_id,
                            target.project_entry_id, current.analysis_domain_bindings),
        owning_identity=identity, identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version="mif-pipeline-saved-result-retirement/v1", runtime_versions=source.runtime_versions,
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=current.configuration_bindings)
    return BatchNodeExecutionResult(context.authorization.authorization_sha256, (artifact,))


def build_saved_result_retirement_codec(spec, *, state_store):
    from .project_stage_codecs import ProjectStageCodec, ProjectStageCodecBinding
    from mif_pipeline.qupath_runtime.scripts import QuPathScriptAsset

    def body(context, target):
        current, binding = _binding_for_context(context, state_store)
        return _project_body(current, binding, target)

    def validate(evidence):
        return validate_saved_result_retirement(evidence, state_store=state_store)

    return ProjectStageCodec.for_stage_owner(
        binding=ProjectStageCodecBinding.create(adapter_spec_sha256=spec.spec_sha256,
            target_script_asset_id=QuPathScriptAsset.RETIRE_SAVED_RESULTS.value,
            wire_stage_id=KIND, terminal_stage_id=KIND, request_codec_version="1"),
        spec=spec, request_projector=body, result_validator=validate)


def accept_completed_edit_retries(
    contexts, *, spec, dependencies, workspace_lease, terminal_committer,
):
    """Accept finished retirement or cell-correction writes without applying them twice.

    A dispatch group may contain new tasks, missing-result retries and completed
    codec-rejected results. We reopen only retry attempts, validate completed
    results against one current project inspection, and commit them together.
    Their original native exchanges remain recorded. Other tasks keep the
    ordinary execution path; no completed write is repeated because of grouping.
    """
    from mif_pipeline.pipeline_core import StageResult
    from mif_pipeline.qupath_runtime.scripts import QuPathScriptAsset
    from .batch_failed_retry import _check_live_target
    from .batch_state import BatchRawNodeOutcome

    if spec.node_kind == KIND:
        asset = QuPathScriptAsset.RETIRE_SAVED_RESULTS.value
        validate = validate_saved_result_retirement
    elif spec.node_kind == "segmentation_correction":
        from .batch_cell_corrections import validate_cell_correction
        asset = QuPathScriptAsset.CORRECT_KNOWN_CELL.value
        validate = validate_cell_correction
    else:
        raise BatchArtifactError("Saved-edit recovery requires its existing task owner.")
    state = dependencies.state_store
    with state.current_project_control(operation="retry", lease=workspace_lease) as control:
        control.project_processes.assert_all_drained(
            batch_id=control.head.batch_id,
            project_identity_sha256=control.head.project_identity_sha256,
        )
        reopener = dependencies.project_stage.committed_exchange_reopener(
            workspace_lease=workspace_lease)
        completed = []
        pending = []
        for context in contexts:
            retry = context.authorization.failed_retry
            if retry is None:
                pending.append(context)
                continue
            previous = reopener.reopen_failed_target_for_owner(
                node_id=context.node_id,
                terminal_record_sha256=retry.terminal_record_sha256,
                expected_binding=(spec.spec_sha256,
                    asset, spec.node_kind, spec.node_kind, "1"),
            )
            if previous.completed_evidence is None:
                pending.append(context)
            else:
                completed.append((context, previous))
        if not completed:
            return (), tuple(pending)

        snapshot = dependencies.project_provider.inspect_live_project(control.project_lease)
        outcomes = []
        for context, previous in completed:
            _check_live_target(state_store=state, head=control.head,
                reopened=previous, snapshot=snapshot, workspace_lease=workspace_lease)
            current = state.authenticate_current_execution_context(context)
            if (context.authorization.failed_retry.terminal_record_sha256
                    != previous.terminal_record_sha256
                    or previous.state_head_sha256 != current.state_head_sha256):
                raise BatchArtifactError("Saved-result recovery is not bound to the current Retry.")
            evidence = previous.completed_evidence
            result = validate(
                evidence, state_store=state, acceptance_context=context)
            outcomes.append(BatchRawNodeOutcome(
                context=context,
                stage_result=StageResult.completed(stage=spec.node_kind,
                    slide_id=previous.authorized.slide_id,
                    generation_id=evidence.wire_stage_result.generation_id),
                execution_result=result,
                execution_exchange_sha256=evidence.execution_exchange_sha256,
            ))
        return terminal_committer.commit_raw(tuple(outcomes)), tuple(pending)
