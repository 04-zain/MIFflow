"""Pure orchestration over authenticated ROI semantic values and files.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass, replace
from enum import Enum
import hashlib
import json
from math import isfinite
import os
from pathlib import Path
from uuid import uuid4

import yaml

from mif_pipeline.orchestration.batch_phenotyping_contracts import (
    BatchROISemanticProfileBinding,
    authenticate_roi_semantic_profile_bytes,
)
from mif_pipeline.orchestration.analysis_services import (
    validate_semantic_evidence_files,
    validate_semantic_raw_inventory_file,
)
from mif_pipeline.cell_analysis import (
    FinalObjectDispositionEvidence,
    ManualSelectionEvidence,
    SemanticResultArtifactEvidence,
    semantic_parent_content_key,
    semantic_secondary_content_key,
)
from mif_pipeline.compartment_analysis import (
    ManualSelectionLineage,
    ObjectInventoryItem,
    SelectionAlgorithm,
    SelectionContext,
    StructureCountDecision,
    StructureCountObservation,
)
from mif_pipeline.pipeline_core.config import ReviewPolicy, StructureCountBounds
from mif_pipeline.pipeline_core.domain import AnalysisDomainRef
from mif_pipeline.pipeline_core.errors import ROIAnalysisError, validate_safe_path_segment
from mif_pipeline.pipeline_core.interactions import (
    resolve_resume_envelope,
    validate_interaction_response,
)
from mif_pipeline.pipeline_core.stage_result import InteractionReason, StageResult
from mif_pipeline.roi_validation import (
    RuleOutcome,
    SemanticEvidenceSnapshot,
    SemanticProfileV3,
    SemanticRegionDecision,
)

# ==============================================================================
#                               Semantic execution
# ==============================================================================


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _mapping_sha256(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _decode_json_mapping(content: bytes, *, label: str) -> dict[str, object]:
    if type(content) is not bytes or not content:
        raise ROIAnalysisError(f"{label} must be nonempty exact bytes")

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite value {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ROIAnalysisError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise ROIAnalysisError(f"{label} must contain one JSON object")
    return value


def _json_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ROIAnalysisError("semantic mappings require string keys")
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    if value is None or type(value) in {str, int, float, bool}:
        return value
    raise ROIAnalysisError("semantic value is not JSON serializable")


def _terminal_semantic_review_interaction(
    *,
    stage: str,
    slide_id: str,
    section_id: str,
    generation_id: str,
) -> StageResult:
    return StageResult.needs_interaction(
        stage=stage,
        slide_id=slide_id,
        generation_id=generation_id,
        reason=InteractionReason.ROI_REVIEW_REQUIRED,
        affected_object_ids=(section_id,),
        message="Review semantic ROI candidates in the current tissue section.",
        allowed_actions=("review_rois", "accept", "edit", "reject"),
        resume_context={
            "schema_version": "semantic-roi-review-interaction/v1",
            "section_id": section_id,
        },
        checkpoint_id="pending-workflow-checkpoint",
    )


def validate_roi_terminal_semantic_review_response(
    interaction: StageResult,
    *,
    action: str,
    response: Mapping[str, object],
) -> Mapping[str, object]:
    """Validate one terminal semantic review response through the UI-neutral owner."""
    if (
        not isinstance(interaction, StageResult)
        or interaction.interaction_reason is not InteractionReason.ROI_REVIEW_REQUIRED
        or interaction.checkpoint_id != "pending-workflow-checkpoint"
        or len(interaction.affected_object_ids) != 1
    ):
        raise ROIAnalysisError("terminal semantic review response requires its offered interaction")
    validated = validate_interaction_response(interaction, action, response)
    service_context = resolve_resume_envelope(interaction.resume_context or {}).service_context
    if set(service_context) != {"schema_version", "section_id"}:
        raise ROIAnalysisError("terminal semantic review context is stale")
    owner_context = {
        "schema_version": service_context.get("schema_version"),
        "section_id": service_context.get("section_id"),
    }
    if owner_context != {
        "schema_version": "semantic-roi-review-interaction/v1",
        "section_id": interaction.affected_object_ids[0],
    }:
        raise ROIAnalysisError("terminal semantic review context is stale")
    return {
        "checkpoint_id": interaction.checkpoint_id,
        "stage": interaction.stage,
        "slide_id": interaction.slide_id,
        "generation_id": interaction.generation_id,
        "resume_context": owner_context,
        "action": validated["action"],
        "response": _json_value(validated["response"]),
    }


def _unattended_terminal_review_is_satisfied(
    output: Mapping[str, object],
) -> bool:
    qc = output.get("semantic_qc")
    if not isinstance(qc, Mapping):
        return False
    ids = qc.get("evaluated_roi_ids")
    if isinstance(ids, (str, bytes)) or not isinstance(ids, Sequence):
        return False
    expected_config_sha256 = output.get("semantic_config_sha256")
    if expected_config_sha256 is None:
        expected_config_sha256 = qc.get("semantic_config_sha256")
    return (
        output.get("validation_status") == "validated"
        and output.get("qc_passed") is True
        and qc.get("status") == "passed"
        and qc.get("result_count") == len(ids)
        and len(set(ids)) == len(ids)
        and _is_sha256(expected_config_sha256)
        and qc.get("semantic_config_sha256") == expected_config_sha256
    )


def resolve_roi_terminal_semantic_review(
    *,
    review_policy: ReviewPolicy,
    preliminary_output: Mapping[str, object],
    authorized_action: object,
    stage: str,
    slide_id: str,
    section_id: str,
    generation_id: str,
) -> str | StageResult:
    """Resolve terminal review or issue its exact resumable interaction."""
    if not isinstance(review_policy, ReviewPolicy):
        raise ROIAnalysisError("semantic terminal review policy is invalid")
    if not isinstance(preliminary_output, Mapping):
        raise ROIAnalysisError("semantic terminal review output is invalid")
    interaction = _terminal_semantic_review_interaction(
        stage=stage,
        slide_id=slide_id,
        section_id=section_id,
        generation_id=generation_id,
    )
    if authorized_action is not None:
        expected = {
            "checkpoint_id": interaction.checkpoint_id,
            "stage": interaction.stage,
            "slide_id": interaction.slide_id,
            "generation_id": interaction.generation_id,
            "resume_context": _json_value(interaction.resume_context or {}),
        }
        if (
            not isinstance(authorized_action, Mapping)
            or set(authorized_action) != {*expected, "action", "response"}
            or any(authorized_action.get(key) != value for key, value in expected.items())
            or authorized_action.get("response") != {}
        ):
            raise ROIAnalysisError("terminal semantic review action is not state-authorized")
        action = authorized_action.get("action")
        if action == "accept":
            return "accepted"
        if action == "reject":
            return "rejected"
        if action not in {"review_rois", "edit"}:
            raise ROIAnalysisError("terminal semantic review action is invalid")
        return interaction
    if review_policy is ReviewPolicy.IF_UNVALIDATED:
        if preliminary_output.get("validation_status") == "validated":
            return "accepted"
    elif review_policy is ReviewPolicy.IF_QC_FAILED:
        if preliminary_output.get("qc_passed") is True:
            return "accepted"
    elif review_policy is ReviewPolicy.UNATTENDED:
        if _unattended_terminal_review_is_satisfied(preliminary_output):
            return "accepted"
    return interaction


def _profile_values(
    *,
    profile_bindings: Sequence[BatchROISemanticProfileBinding],
    profile_bytes: Sequence[bytes],
    expected_validation_domain_id: str,
) -> tuple[SemanticProfileV3, ...]:
    bindings = tuple(profile_bindings)
    contents = tuple(profile_bytes)
    if len(bindings) != len(contents):
        raise ROIAnalysisError("semantic profile bindings and bytes differ")
    profiles: list[SemanticProfileV3] = []
    for binding, content in zip(bindings, contents):
        authenticated = authenticate_roi_semantic_profile_bytes(
            binding,
            content,
            expected_validation_domain_id=expected_validation_domain_id,
        )
        try:
            decoded = yaml.safe_load(authenticated.decode("utf-8"))
            profile = SemanticProfileV3.from_mapping(decoded)
        except (UnicodeDecodeError, yaml.YAMLError, TypeError, ValueError) as exc:
            raise ROIAnalysisError("authenticated semantic profile bytes are invalid") from exc
        if int(profile.schema_version.rsplit("v", 1)[1]) != binding.profile_schema_version:
            raise ROIAnalysisError("semantic profile schema differs from its binding")
        profiles.append(profile)
    by_structure = {profile.structure_id: profile for profile in profiles}
    if len(by_structure) != len(profiles):
        raise ROIAnalysisError("semantic profiles duplicate a structure")
    return tuple(profiles)


def _criterion_result_mapping(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    mapped = _json_value(value)
    if not isinstance(mapped, dict):
        raise ROIAnalysisError("semantic criterion result is not typed")
    return mapped


def validate_roi_semantic_raw_inventory(
    raw_objects_path: Path,
    *,
    raw_export_sha256: str,
    slide_id: str,
    section_id: str,
    analysis_domain_id: str,
    tissue_generation_id: str,
    pipeline_run_id: str,
) -> dict[str, tuple[dict[str, object], ...]]:
    """Validate and stream-project one authenticated domain raw inventory."""
    return validate_semantic_raw_inventory_file(
        raw_objects_path,
        raw_export_sha256=raw_export_sha256,
        slide_id=slide_id,
        section_id=section_id,
        analysis_domain_id=analysis_domain_id,
        tissue_generation_id=tissue_generation_id,
        pipeline_run_id=pipeline_run_id,
    )


def _profile_evidence_selectors(
    profiles: Sequence[SemanticProfileV3],
) -> list[dict[str, object]]:
    output: dict[str, dict[str, object]] = {}

    def visit(node: object) -> None:
        if not isinstance(node, Mapping):
            return
        if "all_of" in node or "any_of" in node:
            for child in next(iter(node.values())):
                visit(child)
            return
        if "at_least" in node:
            for child in node["at_least"]["criteria"]:
                visit(child)
            return
        kind = node.get("type")
        selector = node.get("selector", {})
        if type(kind) is not str or not isinstance(selector, Mapping) or not selector:
            return
        row = {"type": kind, "selector": dict(selector)}
        identity = json.dumps(row, sort_keys=True, separators=(",", ":"))
        output[identity] = row

    for profile in profiles:
        visit(profile.criteria)
        for rule in profile.classification_rules:
            visit(rule.criteria)
    return [output[key] for key in sorted(output)]


def prepare_roi_semantic_evidence_request(
    *,
    raw_inventory: Mapping[str, Sequence[Mapping[str, object]]],
    raw_export_sha256: str,
    profile_bindings: Sequence[BatchROISemanticProfileBinding],
    profile_bytes: Sequence[bytes],
    expected_validation_domain_id: str,
    physical_calibration: Mapping[str, object],
    phenotype_view: Mapping[str, object],
) -> dict[str, object]:
    """Prepare the path-free scientific body for candidate evidence export."""
    if not _is_sha256(raw_export_sha256):
        raise ROIAnalysisError("semantic raw export digest is invalid")
    profiles = _profile_values(
        profile_bindings=profile_bindings,
        profile_bytes=profile_bytes,
        expected_validation_domain_id=expected_validation_domain_id,
    )
    if not all(
        type(physical_calibration.get(key)) in {int, float}
        and isfinite(float(physical_calibration[key]))
        and float(physical_calibration[key]) > 0
        for key in ("x_um_per_px", "y_um_per_px")
    ):
        raise ROIAnalysisError("semantic evidence requires positive physical calibration")
    if any(
        type(phenotype_view.get(key)) is not str or not phenotype_view[key]
        for key in ("mode", "generation_id", "inventory_sha256")
    ) or not _is_sha256(phenotype_view.get("inventory_sha256")):
        raise ROIAnalysisError("semantic evidence phenotype view is invalid")
    primaries = raw_inventory.get("primary")
    if isinstance(primaries, (str, bytes)) or not isinstance(primaries, Sequence):
        raise ROIAnalysisError("semantic raw primary inventory is invalid")
    primary_rows = []
    for item in primaries:
        if not isinstance(item, Mapping):
            raise ROIAnalysisError("semantic raw primary inventory row is invalid")
        primary_rows.append(
            {
                "roi_id": item.get("roi_id"),
                "geometry_digest": item.get("geometry_sha256"),
                "primary_class_id": item.get("primary_class_id"),
            }
        )
    primary_rows.sort(key=lambda item: str(item["roi_id"]))
    return {
        "raw_export_sha256": raw_export_sha256,
        "physical_calibration": {
            "x_um_per_px": float(physical_calibration["x_um_per_px"]),
            "y_um_per_px": float(physical_calibration["y_um_per_px"]),
        },
        "primary_geometry_inventory_sha256": _mapping_sha256(
            {
                "primary_geometry_inventory/v1": primary_rows,
            }
        ),
        "phenotype_view": dict(phenotype_view),
        "evidence_selectors": _profile_evidence_selectors(profiles),
    }


def validate_roi_semantic_evidence_response(
    *,
    raw_objects_path: Path,
    raw_export_sha256: str,
    membership_artifact_path: Path,
    membership_artifact_sha256: str,
    slide_id: str,
    section_id: str,
    analysis_domain_id: str,
    tissue_generation_id: str,
    pipeline_run_id: str,
    segmentation_population_id: str,
    physical_calibration: Mapping[str, object],
    phenotype_view: Mapping[str, object],
) -> tuple[SemanticEvidenceSnapshot, ...]:
    """Authenticate file-backed evidence through the strict streaming owner."""
    return validate_semantic_evidence_files(
        raw_objects_path,
        membership_artifact_path,
        raw_export_sha256=raw_export_sha256,
        membership_artifact_sha256=membership_artifact_sha256,
        slide_id=slide_id,
        section_id=section_id,
        analysis_domain_id=analysis_domain_id,
        tissue_generation_id=tissue_generation_id,
        pipeline_run_id=pipeline_run_id,
        segmentation_population_id=segmentation_population_id,
        physical_calibration=physical_calibration,
        phenotype_view=phenotype_view,
    )


def _final_count_inventory(
    result_rows: Sequence[Mapping[str, object]],
    raw_inventory: Mapping[str, Sequence[Mapping[str, object]]],
) -> list[dict[str, object]]:
    secondary = raw_inventory.get("secondary")
    if isinstance(secondary, (str, bytes)) or not isinstance(secondary, Sequence):
        raise ROIAnalysisError("semantic raw secondary inventory is invalid")
    candidates = {str(item.get("roi_id")): item for item in secondary if isinstance(item, Mapping)}
    if len(candidates) != len(secondary):
        raise ROIAnalysisError("semantic raw secondary inventory is invalid")
    values: list[dict[str, object]] = []
    for raw in result_rows:
        if not isinstance(raw, Mapping):
            raise ROIAnalysisError("semantic result row is invalid")
        candidate = candidates.get(str(raw.get("roi_id")))
        if candidate is None:
            raise ROIAnalysisError("semantic count inventory is missing a result ROI")
        values.append(
            {
                **dict(raw),
                "content_key": candidate["content_key"],
                "parent_content_key": candidate["parent_content_key"],
            }
        )
    if set(candidates) != {str(item.get("roi_id")) for item in result_rows}:
        raise ROIAnalysisError("semantic count candidate inventory is incomplete")
    return values


def _count_decision_mapping(decision: StructureCountDecision) -> dict[str, object]:
    mapped = _json_value(decision)
    if not isinstance(mapped, dict):
        raise ROIAnalysisError("semantic count decision is not typed")
    unsigned = {"schema_version": "structure-count-decision/v1", **mapped}
    return {**unsigned, "decision_sha256": _mapping_sha256(unsigned)}


def resolve_roi_semantic_count(
    *,
    domain: AnalysisDomainRef,
    result_rows: Sequence[Mapping[str, object]],
    raw_inventory: Mapping[str, Sequence[Mapping[str, object]]],
    policies: Mapping[str, StructureCountBounds],
    selection_contexts: Mapping[str, SelectionContext],
    manual_selections: Mapping[str, ManualSelectionLineage] | None = None,
) -> dict[str, object]:
    """Resolve final counts or return the next exact manual-cap requirement."""
    from .workflow import evaluate_finalized_secondary_count_policy

    if not isinstance(domain, AnalysisDomainRef):
        raise ROIAnalysisError("semantic count domain is invalid")
    if (
        not isinstance(policies, Mapping)
        or not policies
        or any(
            type(key) is not str or not isinstance(value, StructureCountBounds)
            for key, value in policies.items()
        )
    ):
        raise ROIAnalysisError("semantic count policies are invalid")
    final_inventory = _final_count_inventory(result_rows, raw_inventory)
    accepted_by_structure: dict[str, list[dict[str, object]]] = {key: [] for key in policies}
    for row in final_inventory:
        if row.get("semantic_status") == "accepted" and row.get("review_status") == "accepted":
            structure_id = row.get("structure_id")
            if structure_id not in accepted_by_structure:
                raise ROIAnalysisError("final semantic inventory references an unknown structure")
            accepted_by_structure[str(structure_id)].append(row)
    has_failed_minimum = any(
        len(accepted_by_structure[key]) < policy.minimum for key, policy in policies.items()
    )
    provided_manual = {} if manual_selections is None else manual_selections
    if not isinstance(provided_manual, Mapping):
        raise ROIAnalysisError("manual semantic selections are invalid")
    if not has_failed_minimum:
        for structure_id in sorted(policies):
            policy = policies[structure_id]
            rows = accepted_by_structure[structure_id]
            if (
                policy.maximum is not None
                and len(rows) > policy.maximum
                and getattr(policy.excess_policy, "value", None) == "manual_select"
                and structure_id not in provided_manual
            ):
                return {
                    "count_decision": None,
                    "count_decision_mapping": None,
                    "manual_selection_requirement": {
                        "structure_id": structure_id,
                        "maximum": policy.maximum,
                        "observed_count": len(rows),
                        "inventory": tuple(rows),
                    },
                    "active_secondary_roi_ids": None,
                    "final_object_dispositions": None,
                }
    decision = evaluate_finalized_secondary_count_policy(
        domain=domain,
        final_inventory=final_inventory,
        policies=policies,
        selection_contexts=selection_contexts,
        manual_selections=provided_manual,
    )
    if not isinstance(decision, StructureCountDecision):
        raise ROIAnalysisError("semantic count owner returned an invalid decision")
    decision_mapping = _count_decision_mapping(decision)
    included = set(decision.included_content_keys)
    secondary = raw_inventory["secondary"]
    by_content_key = {str(item["content_key"]): item for item in secondary}
    if not included <= set(by_content_key):
        raise ROIAnalysisError("semantic count decision references an unknown ROI")
    accepted_roi_ids = {
        str(row["roi_id"])
        for row in result_rows
        if row.get("semantic_status") == "accepted" and row.get("review_status") == "accepted"
    }
    active_secondary_roi_ids = sorted(str(by_content_key[key]["roi_id"]) for key in included)
    if not set(active_secondary_roi_ids) <= accepted_roi_ids:
        raise ROIAnalysisError("semantic count decision activated a rejected ROI")
    dispositions: list[dict[str, object]] = []
    for output in decision_mapping["outputs"]:
        if not isinstance(output, Mapping):
            raise ROIAnalysisError("semantic count decision output is invalid")
        inventory = output.get("inventory")
        if isinstance(inventory, (str, bytes)) or not isinstance(inventory, Sequence):
            raise ROIAnalysisError("semantic count decision inventory is invalid")
        for item in inventory:
            if not isinstance(item, Mapping):
                raise ROIAnalysisError("semantic count decision item is invalid")
            candidate = by_content_key.get(str(item.get("content_key")))
            if candidate is None:
                raise ROIAnalysisError(
                    "semantic count decision content key has no raw ROI identity"
                )
            lineage = item.get("selection_lineage")
            dispositions.append(
                FinalObjectDispositionEvidence.create(
                    content_key=item.get("content_key"),
                    roi_id=candidate.get("roi_id"),
                    path_object_id=candidate.get("path_object_id"),
                    structure_id=item.get("structure_id"),
                    parent_content_key=item.get("parent_content_key"),
                    geometry_sha256=item.get("geometry_sha256"),
                    disposition=item.get("disposition"),
                    selection_lineage_sha256=(
                        None if lineage is None else _mapping_sha256(lineage)
                    ),
                ).to_mapping()
            )
    dispositions.sort(key=lambda item: str(item["content_key"]))
    return {
        "count_decision": decision,
        "count_decision_mapping": decision_mapping,
        "manual_selection_requirement": None,
        "active_secondary_roi_ids": active_secondary_roi_ids,
        "final_object_dispositions": dispositions,
    }


def _canonical_artifact_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _publish_immutable_artifact(
    path: Path,
    content: bytes,
    *,
    label: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise ROIAnalysisError(f"immutable {label} was replaced")
        return
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise ROIAnalysisError(f"{label} publication conflicted")
    finally:
        temporary.unlink(missing_ok=True)


def _manual_lineage_mapping(
    lineage: ManualSelectionLineage,
) -> dict[str, object]:
    mapped = _json_value(lineage)
    if not isinstance(mapped, dict):
        raise ROIAnalysisError("manual selection lineage is not serializable")
    return mapped


def _validated_prior_manual_authority(
    *,
    raw_lineages: object,
    raw_evidence: object,
    analysis_domain_sha256: str,
    config_sha256: str,
    result_sha256: str,
    current_structure_id: str,
) -> tuple[
    dict[str, ManualSelectionLineage],
    tuple[ManualSelectionEvidence, ...],
]:
    if not isinstance(raw_lineages, Mapping) or any(type(key) is not str for key in raw_lineages):
        raise ROIAnalysisError("prior manual selection lineages are invalid")
    if isinstance(raw_evidence, (str, bytes)) or not isinstance(raw_evidence, Sequence):
        raise ROIAnalysisError("prior manual selection evidence is invalid")
    lineages: dict[str, ManualSelectionLineage] = {}
    for structure_id in sorted(raw_lineages):
        raw = raw_lineages[structure_id]
        try:
            lineage = (
                raw
                if isinstance(raw, ManualSelectionLineage)
                else ManualSelectionLineage(**raw) if isinstance(raw, Mapping) else None
            )
        except (TypeError, ValueError, ROIAnalysisError) as exc:
            raise ROIAnalysisError("prior manual selection lineage is invalid") from exc
        if (
            lineage is None
            or lineage.structure_id != structure_id
            or (
                lineage.analysis_domain_sha256,
                lineage.config_sha256,
                lineage.result_sha256,
            )
            != (analysis_domain_sha256, config_sha256, result_sha256)
        ):
            raise ROIAnalysisError("prior manual selection lineage is stale")
        lineages[structure_id] = lineage
    if current_structure_id in lineages:
        raise ROIAnalysisError("manual selection already contains the current structure")
    evidence: list[ManualSelectionEvidence] = []
    for raw in raw_evidence:
        try:
            item = (
                raw
                if isinstance(raw, ManualSelectionEvidence)
                else ManualSelectionEvidence.from_mapping(raw)
            )
        except (TypeError, ValueError, ROIAnalysisError) as exc:
            raise ROIAnalysisError("prior manual selection evidence is invalid") from exc
        evidence.append(item)
    evidence_ids = tuple(item.structure_id for item in evidence)
    if (
        len(set(evidence_ids)) != len(evidence_ids)
        or evidence_ids != tuple(sorted(evidence_ids))
        or set(evidence_ids) != set(lineages)
    ):
        raise ROIAnalysisError("prior manual selection lineage and evidence inventory differ")
    for item in evidence:
        if _json_value(item.selection_lineage) != _manual_lineage_mapping(
            lineages[item.structure_id]
        ):
            raise ROIAnalysisError("prior manual selection evidence lineage was replaced")
    return lineages, tuple(evidence)


def build_roi_semantic_manual_cap_interaction(
    *,
    requirement: Mapping[str, object],
    prior_lineages: Mapping[str, ManualSelectionLineage],
    prior_evidence: Sequence[ManualSelectionEvidence],
    output_dir: Path,
    slide_id: str,
    section_id: str,
    analysis_domain: AnalysisDomainRef,
    analysis_domain_revision: int,
    analysis_domain_revision_sha256: str,
    pipeline_run_id: str,
    config_sha256: str,
    semantic_result_set_sha256: str,
    semantic_count_state_descriptor: Mapping[str, object],
) -> StageResult:
    """Publish one manual-cap inventory and return its closed interaction."""
    if not isinstance(requirement, Mapping) or set(requirement) != {
        "structure_id",
        "maximum",
        "observed_count",
        "inventory",
    }:
        raise ROIAnalysisError("manual selection requirement is invalid")
    if not isinstance(analysis_domain, AnalysisDomainRef) or (
        analysis_domain.slide_id != slide_id or analysis_domain.section_id != section_id
    ):
        raise ROIAnalysisError("manual selection analysis domain is stale")
    structure_id = requirement.get("structure_id")
    maximum = requirement.get("maximum")
    observed_count = requirement.get("observed_count")
    rows = requirement.get("inventory")
    if (
        not isinstance(structure_id, str)
        or not structure_id
        or type(maximum) is not int
        or maximum < 1
        or type(observed_count) is not int
        or observed_count <= maximum
        or isinstance(rows, (str, bytes))
        or not isinstance(rows, Sequence)
        or len(rows) != observed_count
    ):
        raise ROIAnalysisError("manual selection requirement is invalid")
    lineages, evidence = _validated_prior_manual_authority(
        raw_lineages=prior_lineages,
        raw_evidence=prior_evidence,
        analysis_domain_sha256=analysis_domain_revision_sha256,
        config_sha256=config_sha256,
        result_sha256=semantic_result_set_sha256,
        current_structure_id=structure_id,
    )
    row_by_content_key: dict[str, Mapping[str, object]] = {}
    items: list[ObjectInventoryItem] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ROIAnalysisError("manual selection inventory row is invalid")
        content_key = str(row.get("content_key"))
        item = ObjectInventoryItem(
            content_key=content_key,
            structure_id=structure_id,
            parent_content_key=row.get("parent_content_key"),
            geometry_sha256=row.get("geometry_sha256"),
        )
        if content_key in row_by_content_key:
            raise ROIAnalysisError("manual selection inventory duplicates a content key")
        row_by_content_key[content_key] = row
        items.append(item)
    observation = StructureCountObservation(
        output_id=structure_id,
        count=len(items),
        inventory=tuple(items),
    )
    canonical_rows = tuple(
        {
            "content_key": item.content_key,
            "roi_id": str(row_by_content_key[item.content_key]["roi_id"]),
            "parent_content_key": item.parent_content_key,
            "geometry_sha256": item.geometry_sha256,
        }
        for item in observation.inventory
    )
    payload = {
        "schema_version": "structure-count-inventory/v1",
        "slide_id": slide_id,
        "section_id": section_id,
        "analysis_domain_id": analysis_domain.analysis_domain_id,
        "analysis_domain_revision": analysis_domain_revision,
        "analysis_domain_revision_sha256": analysis_domain_revision_sha256,
        "tissue_generation_id": analysis_domain.tissue_generation_id,
        "pipeline_run_id": pipeline_run_id,
        "config_sha256": config_sha256,
        "semantic_result_set_sha256": semantic_result_set_sha256,
        "structure_id": structure_id,
        "maximum": maximum,
        "observed_count": observation.count,
        "inventory_sha256": observation.inventory_sha256,
        "selection_algorithm": "manual-select/v1",
        "items": canonical_rows,
    }
    content = _canonical_artifact_bytes(payload)
    file_sha256 = hashlib.sha256(content).hexdigest()
    domain_id = validate_safe_path_segment(analysis_domain.analysis_domain_id, "analysis_domain_id")
    run_id = validate_safe_path_segment(pipeline_run_id, "run_id")
    structure_segment = validate_safe_path_segment(structure_id, "structure_id")
    path = (
        Path(output_dir)
        / "semantic-results"
        / run_id
        / domain_id
        / semantic_result_set_sha256
        / f"manual-{structure_segment}-{observation.inventory_sha256}.json"
    )
    _publish_immutable_artifact(
        path,
        content,
        label="manual secondary-selection inventory",
    )
    descriptor = {
        "artifact_id": (
            f"manual-secondary-{domain_id[:12]}-{structure_segment}-"
            f"{observation.inventory_sha256[:12]}"
        ),
        "schema_version": "structure-count-inventory/v1",
        "path": str(path.resolve()),
        "sha256": file_sha256,
        "record_count": observation.count,
    }
    context: dict[str, object] = {
        "schema_version": "secondary-structure-selection/v1",
        "slide_id": slide_id,
        "section_id": section_id,
        "analysis_domain_id": analysis_domain.analysis_domain_id,
        "analysis_domain_revision": analysis_domain_revision,
        "analysis_domain_revision_sha256": analysis_domain_revision_sha256,
        "tissue_generation_id": analysis_domain.tissue_generation_id,
        "pipeline_run_id": pipeline_run_id,
        "config_sha256": config_sha256,
        "semantic_result_set_sha256": semantic_result_set_sha256,
        "structure_id": structure_id,
        "maximum": maximum,
        "observed_count": observation.count,
        "inventory_sha256": observation.inventory_sha256,
        "selection_algorithm": "manual-select/v1",
        "inventory_descriptor": descriptor,
        "semantic_count_state_descriptor": _json_value(semantic_count_state_descriptor),
    }
    if lineages:
        context["prior_manual_selection_lineages"] = {
            key: _manual_lineage_mapping(lineages[key]) for key in sorted(lineages)
        }
        context["prior_manual_selection_evidence"] = [item.to_mapping() for item in evidence]
    checkpoint_sha256 = _mapping_sha256(context)
    return StageResult.needs_interaction(
        stage="semantic_validation",
        slide_id=slide_id,
        generation_id=analysis_domain.tissue_generation_id,
        reason=InteractionReason.SECONDARY_STRUCTURE_SELECTION_REQUIRED,
        affected_object_ids=(),
        message=(
            f"Select exactly {maximum} canonical {structure_id} "
            f"content keys from the checksummed inventory of {len(canonical_rows)}."
        ),
        allowed_actions=("accept_secondary_structure_selection",),
        resume_context=context,
        checkpoint_id="manual-secondary-selection-" + checkpoint_sha256[:24],
    )


def validate_roi_semantic_manual_cap_response(
    interaction: StageResult,
    *,
    action: str,
    response: Mapping[str, object],
) -> tuple[
    Mapping[str, ManualSelectionLineage],
    tuple[ManualSelectionEvidence, ...],
]:
    """Validate one manual cap and return its full canonical predecessor chain."""
    if (
        not isinstance(interaction, StageResult)
        or interaction.interaction_reason
        is not InteractionReason.SECONDARY_STRUCTURE_SELECTION_REQUIRED
    ):
        raise ROIAnalysisError("manual semantic cap response requires its offered interaction")
    validated = validate_interaction_response(interaction, action, response)
    context = resolve_resume_envelope(interaction.resume_context or {}).service_context
    structure_id = context.get("structure_id")
    if not isinstance(structure_id, str) or not structure_id:
        raise ROIAnalysisError("manual semantic cap structure is invalid")
    lineages, evidence = _validated_prior_manual_authority(
        raw_lineages=context.get("prior_manual_selection_lineages", {}),
        raw_evidence=context.get("prior_manual_selection_evidence", ()),
        analysis_domain_sha256=str(context.get("analysis_domain_revision_sha256")),
        config_sha256=str(context.get("config_sha256")),
        result_sha256=str(context.get("semantic_result_set_sha256")),
        current_structure_id=structure_id,
    )
    normalized = validated.get("response")
    if not isinstance(normalized, Mapping):
        raise ROIAnalysisError("manual semantic cap response is invalid")
    try:
        lineage = ManualSelectionLineage(
            analysis_domain_sha256=str(context["analysis_domain_revision_sha256"]),
            config_sha256=str(context["config_sha256"]),
            result_sha256=str(context["semantic_result_set_sha256"]),
            inventory_sha256=str(context["inventory_sha256"]),
            structure_id=structure_id,
            algorithm=SelectionAlgorithm.MANUAL_SELECT_V1,
            selected_content_keys=tuple(normalized["selected_content_keys"]),
            selection_criterion=str(normalized["selection_criterion"]),
            reviewer_id=str(normalized["reviewer_id"]),
            rationale=str(normalized["rationale"]),
        )
        mapped = _manual_lineage_mapping(lineage)
        descriptor = context["inventory_descriptor"]
        if not isinstance(descriptor, Mapping):
            raise ROIAnalysisError("manual semantic cap inventory descriptor is invalid")
        current_evidence = ManualSelectionEvidence.create(
            structure_id=structure_id,
            inventory_descriptor=descriptor,
            selection_lineage=mapped,
        )
    except (KeyError, TypeError, ValueError, ROIAnalysisError) as exc:
        raise ROIAnalysisError("manual semantic cap lineage is invalid") from exc
    accumulated_lineages = {**lineages, structure_id: lineage}
    accumulated_evidence = (*evidence, current_evidence)
    return (
        dict(sorted(accumulated_lineages.items())),
        tuple(sorted(accumulated_evidence, key=lambda item: item.structure_id)),
    )


def serialize_roi_semantic_result_artifact(
    *,
    result_rows: Sequence[Mapping[str, object]],
    result_set_sha256: str,
) -> bytes:
    """Serialize the existing semantic-result-set/v1 artifact exactly once."""
    if not _is_sha256(result_set_sha256):
        raise ROIAnalysisError("semantic result-set digest is invalid")
    return _canonical_artifact_bytes(
        {
            "schema_version": "semantic-result-set/v1",
            "semantic_result_set_sha256": result_set_sha256,
            "semantic_results": [_json_value(row) for row in result_rows],
        }
    )


def publish_roi_semantic_result_artifact(
    *,
    output_dir: Path,
    pipeline_run_id: str,
    analysis_domain_id: str,
    result_rows: Sequence[Mapping[str, object]],
    result_set_sha256: str,
) -> SemanticResultArtifactEvidence:
    """Publish one immutable content-addressed semantic-result-set/v1 artifact."""
    content = serialize_roi_semantic_result_artifact(
        result_rows=result_rows,
        result_set_sha256=result_set_sha256,
    )
    domain_id = validate_safe_path_segment(analysis_domain_id, "analysis_domain_id")
    run_id = validate_safe_path_segment(pipeline_run_id, "run_id")
    path = Path(output_dir) / "semantic-results" / run_id / domain_id / f"{result_set_sha256}.json"
    _publish_immutable_artifact(
        path,
        content,
        label="semantic result artifact",
    )
    return SemanticResultArtifactEvidence(
        schema_version="semantic-result-set/v1",
        path=str(path.resolve()),
        sha256=hashlib.sha256(content).hexdigest(),
        record_count=len(result_rows),
        semantic_result_set_sha256=result_set_sha256,
    )


_SEMANTIC_MUTATION_FIELDS = (
    "roi_id",
    "slide_id",
    "section_id",
    "analysis_domain_id",
    "path_object_id",
    "structure_id",
    "parent_roi_id",
    "semantic_profile",
    "semantic_status",
    "semantic_rule_results",
    "semantic_classification_status",
    "semantic_class_id",
    "semantic_classification_rule_results",
    "review_status",
    "profile_id",
    "profile_version",
    "profile_content_sha256",
    "evidence_snapshot_sha256",
    "geometry_sha256",
)


def authenticate_roi_semantic_result_artifact(
    *,
    result_rows: Sequence[Mapping[str, object]] | None,
    result_set_sha256: str,
    result_artifact: SemanticResultArtifactEvidence,
    result_artifact_bytes: bytes | None,
    analysis_domain_id: str,
    section_id: str,
    tissue_generation_id: str,
    pipeline_run_id: str,
) -> tuple[dict[str, object], ...]:
    if (
        not isinstance(result_artifact, SemanticResultArtifactEvidence)
        or type(result_artifact_bytes) is not bytes
        or not result_artifact_bytes
        or hashlib.sha256(result_artifact_bytes).hexdigest() != result_artifact.sha256
    ):
        raise ROIAnalysisError("semantic finalization result artifact authentication failed")
    payload = _decode_json_mapping(
        result_artifact_bytes,
        label="semantic result artifact",
    )
    artifact_rows = payload.get("semantic_results")
    if (
        set(payload)
        != {
            "schema_version",
            "semantic_result_set_sha256",
            "semantic_results",
        }
        or payload.get("schema_version") != "semantic-result-set/v1"
        or payload.get("semantic_result_set_sha256") != result_set_sha256
        or isinstance(artifact_rows, (str, bytes))
        or not isinstance(artifact_rows, Sequence)
        or not all(isinstance(item, Mapping) for item in artifact_rows)
    ):
        raise ROIAnalysisError("semantic finalization result artifact is invalid")
    authenticated = tuple(dict(_json_value(item)) for item in artifact_rows)
    supplied = (
        None if result_rows is None else tuple(dict(_json_value(item)) for item in result_rows)
    )
    if (
        not _is_sha256(result_set_sha256)
        or result_artifact.semantic_result_set_sha256 != result_set_sha256
        or result_artifact.record_count != len(authenticated)
        or _mapping_sha256(
            {
                "schema_version": "semantic-result-set/v1",
                "semantic_results": list(authenticated),
            }
        )
        != result_set_sha256
        or supplied is not None
        and supplied != authenticated
    ):
        raise ROIAnalysisError("semantic finalization result binding is stale")
    expected_scope = {
        "section_id": section_id,
        "analysis_domain_id": analysis_domain_id,
        "tissue_generation_id": tissue_generation_id,
        "pipeline_run_id": pipeline_run_id,
    }
    if any(
        row.get("schema_version") not in {"semantic-result/v2", "semantic-result/v3"}
        or any(row.get(field) != value for field, value in expected_scope.items())
        for row in authenticated
    ):
        raise ROIAnalysisError("semantic finalization result scope is stale")
    return authenticated


def _bind_semantic_count_authority(
    *,
    result_rows: Sequence[Mapping[str, object]],
    count_decision: StructureCountDecision | None,
    final_object_dispositions: Sequence[FinalObjectDispositionEvidence],
    active_secondary_roi_ids: Sequence[str],
    analysis_domain_id: str,
    section_id: str,
    tissue_generation_id: str,
    review_status: str,
) -> list[str]:
    if not isinstance(count_decision, StructureCountDecision):
        raise ROIAnalysisError("semantic finalization count authority is invalid")
    domain = count_decision.domain
    if (
        domain.analysis_domain_id,
        domain.section_id,
        domain.tissue_generation_id,
    ) != (
        analysis_domain_id,
        section_id,
        tissue_generation_id,
    ) or any(row.get("slide_id") != domain.slide_id for row in result_rows):
        raise ROIAnalysisError("semantic finalization count scope is stale")
    dispositions = tuple(final_object_dispositions)
    if not all(isinstance(item, FinalObjectDispositionEvidence) for item in dispositions):
        raise ROIAnalysisError("semantic finalization disposition authority is invalid")
    by_content_key = {item.content_key: item for item in dispositions}
    if len(by_content_key) != len(dispositions):
        raise ROIAnalysisError("semantic finalization dispositions duplicate content identity")
    decision_inventory: dict[str, object] = {}
    for output in count_decision.outputs:
        for item in output.inventory:
            if item.content_key in decision_inventory:
                raise ROIAnalysisError(
                    "semantic finalization count inventory duplicates content identity"
                )
            decision_inventory[item.content_key] = item
    if set(decision_inventory) != set(by_content_key):
        raise ROIAnalysisError("semantic finalization count/disposition authority differs")
    result_by_roi = {
        str(row.get("roi_id")): row
        for row in result_rows
        if isinstance(row, Mapping)
        and row.get("semantic_status") == "accepted"
        and row.get("review_status") == "accepted"
    }
    if len(result_by_roi) != len(dispositions) or set(result_by_roi) != {
        item.roi_id for item in dispositions
    }:
        raise ROIAnalysisError("semantic finalization dispositions differ from accepted results")
    for content_key, disposition in by_content_key.items():
        row = result_by_roi[disposition.roi_id]
        parent_content_key = semantic_parent_content_key(
            analysis_domain_id=analysis_domain_id,
            parent_roi_id=str(row.get("parent_roi_id")),
        )
        expected_content_key = semantic_secondary_content_key(
            analysis_domain_id=analysis_domain_id,
            roi_id=disposition.roi_id,
            path_object_id=str(row.get("path_object_id")),
            structure_id=str(row.get("structure_id")),
            parent_content_key=parent_content_key,
            geometry_sha256=str(row.get("geometry_sha256")),
        )
        decision_item = decision_inventory[content_key]
        lineage = getattr(decision_item, "selection_lineage", None)
        if (
            content_key != expected_content_key
            or disposition.path_object_id != row.get("path_object_id")
            or disposition.structure_id != row.get("structure_id")
            or disposition.parent_content_key != parent_content_key
            or disposition.geometry_sha256 != row.get("geometry_sha256")
            or getattr(decision_item, "structure_id", None) != disposition.structure_id
            or getattr(decision_item, "parent_content_key", None) != disposition.parent_content_key
            or getattr(decision_item, "geometry_sha256", None) != disposition.geometry_sha256
            or _json_value(getattr(decision_item, "disposition", None)) != disposition.disposition
            or disposition.selection_lineage_sha256
            != (None if lineage is None else _mapping_sha256(_json_value(lineage)))
        ):
            raise ROIAnalysisError("semantic finalization disposition identity is stale")
    active = list(active_secondary_roi_ids)
    expected_active = sorted(
        item.roi_id for item in dispositions if item.disposition == "active_included"
    )
    included = set(count_decision.included_content_keys)
    if (
        any(type(item) is not str or not item for item in active)
        or active != sorted(set(active))
        or active != expected_active
        or included
        != {item.content_key for item in dispositions if item.disposition == "active_included"}
        or any(row.get("schema_version") != "semantic-result/v3" and row.get("review_status") != review_status for row in result_rows)
    ):
        raise ROIAnalysisError("semantic active ROI authority is invalid")
    return active


def prepare_roi_semantic_finalization_request(
    *,
    result_rows: Sequence[Mapping[str, object]],
    result_set_sha256: str,
    result_artifact: SemanticResultArtifactEvidence,
    result_artifact_bytes: bytes | None = None,
    count_decision: StructureCountDecision | None = None,
    final_object_dispositions: Sequence[FinalObjectDispositionEvidence] = (),
    active_secondary_roi_ids: Sequence[str],
    analysis_domain_id: str,
    section_id: str,
    tissue_generation_id: str,
    pipeline_run_id: str,
    review_status: str,
    review_action: str | None,
    secondary_semantics_applicable: bool = True,
) -> dict[str, object]:
    """Prepare the complete path-free semantic mutation body."""
    if secondary_semantics_applicable is not True:
        raise ROIAnalysisError("semantic finalization is not applicable")
    authenticated_rows = authenticate_roi_semantic_result_artifact(
        result_rows=result_rows,
        result_set_sha256=result_set_sha256,
        result_artifact=result_artifact,
        result_artifact_bytes=result_artifact_bytes,
        analysis_domain_id=analysis_domain_id,
        section_id=section_id,
        tissue_generation_id=tissue_generation_id,
        pipeline_run_id=pipeline_run_id,
    )
    if (review_status, review_action) not in {
        ("accepted", None),
        ("accepted", "accept"),
        ("rejected", "reject"),
    }:
        raise ROIAnalysisError("semantic finalization review identity is invalid")
    if all(row.get("schema_version") == "semantic-result/v3" for row in authenticated_rows) and (
        review_status != "accepted" or review_action is not None
    ):
        raise ROIAnalysisError("Unified region review requires the completed individual decisions")
    active = _bind_semantic_count_authority(
        result_rows=authenticated_rows,
        count_decision=count_decision,
        final_object_dispositions=final_object_dispositions,
        active_secondary_roi_ids=active_secondary_roi_ids,
        analysis_domain_id=analysis_domain_id,
        section_id=section_id,
        tissue_generation_id=tissue_generation_id,
        review_status=review_status,
    )
    mutations: list[dict[str, object]] = []
    for row in authenticated_rows:
        if not isinstance(row, Mapping) or any(
            field not in row for field in _SEMANTIC_MUTATION_FIELDS
        ):
            raise ROIAnalysisError("semantic result cannot be projected for mutation")
        if row.get("schema_version") != "semantic-result/v3" and row.get("review_status") != review_status:
            raise ROIAnalysisError("semantic result rows disagree with final review status")
        mutations.append({field: _json_value(row[field]) for field in
                          (*_SEMANTIC_MUTATION_FIELDS, *(("region_decision",) if row.get("schema_version") == "semantic-result/v3" else ()))})
    artifact_mapping = result_artifact.to_mapping()
    return {
        "semantic_results": mutations,
        "semantic_finalization": {
            "schema_version": "semantic-finalization/v2",
            "analysis_domain_id": analysis_domain_id,
            "section_id": section_id,
            "tissue_generation_id": tissue_generation_id,
            "pipeline_run_id": pipeline_run_id,
            "semantic_result_set_sha256": result_set_sha256,
            "semantic_result_artifact": artifact_mapping,
            "active_secondary_roi_ids": active,
        },
        "terminal_review_status": review_status,
    }


def validate_roi_semantic_finalization_response(
    output: Mapping[str, object],
    *,
    result_rows: Sequence[Mapping[str, object]],
    result_set_sha256: str,
    result_artifact: SemanticResultArtifactEvidence,
    result_artifact_bytes: bytes | None = None,
    count_decision: StructureCountDecision | None = None,
    final_object_dispositions: Sequence[FinalObjectDispositionEvidence] = (),
    active_secondary_roi_ids: Sequence[str],
    analysis_domain_id: str,
    section_id: str,
    tissue_generation_id: str,
    pipeline_run_id: str,
    expected_review_status: str,
    expected_review_action: str | None,
) -> dict[str, object]:
    """Validate one native receipt against the exact prepared authority.

    The required mapping shape includes ``schema_version``, ``analysis_domain_id``,
    ``section_id``, ``tissue_generation_id``, ``pipeline_run_id``,
    ``semantic_result_set_sha256``. Field checks are applied before the validated value is
    returned or passed to its next constructor."""
    authenticated_rows = authenticate_roi_semantic_result_artifact(
        result_rows=result_rows,
        result_set_sha256=result_set_sha256,
        result_artifact=result_artifact,
        result_artifact_bytes=result_artifact_bytes,
        analysis_domain_id=analysis_domain_id,
        section_id=section_id,
        tissue_generation_id=tissue_generation_id,
        pipeline_run_id=pipeline_run_id,
    )
    active = _bind_semantic_count_authority(
        result_rows=authenticated_rows,
        count_decision=count_decision,
        final_object_dispositions=final_object_dispositions,
        active_secondary_roi_ids=active_secondary_roi_ids,
        analysis_domain_id=analysis_domain_id,
        section_id=section_id,
        tissue_generation_id=tissue_generation_id,
        review_status=expected_review_status,
    )
    if not isinstance(output, Mapping):
        raise ROIAnalysisError("semantic finalization output is invalid")
    receipt = output.get("semantic_finalization")
    required = {
        "schema_version",
        "analysis_domain_id",
        "section_id",
        "tissue_generation_id",
        "pipeline_run_id",
        "semantic_result_set_sha256",
        "semantic_result_artifact",
        "active_secondary_roi_ids",
        "accepted_secondary_roi_ids",
        "accepted_secondary_count",
        "reconciled_cell_ids_sha256",
        "membership_cell_count",
    }
    if not isinstance(receipt, Mapping) or set(receipt) != required:
        raise ROIAnalysisError("semantic finalization receipt fields are invalid")
    expected_active_values = tuple(active_secondary_roi_ids)
    if any(type(item) is not str or not item for item in expected_active_values) or list(
        expected_active_values
    ) != sorted(set(expected_active_values)):
        raise ROIAnalysisError("semantic active ROI inventory is invalid")
    reported_active = receipt.get("active_secondary_roi_ids")
    accepted = receipt.get("accepted_secondary_roi_ids")
    cell_digest = receipt.get("reconciled_cell_ids_sha256")
    cell_count = receipt.get("membership_cell_count")
    artifact_mapping = result_artifact.to_mapping()
    if (
        (expected_review_status, expected_review_action)
        not in {
            ("accepted", None),
            ("accepted", "accept"),
            ("rejected", "reject"),
        }
        or output.get("review_status") != expected_review_status
        or output.get("review_action") != expected_review_action
        or receipt.get("schema_version") != "semantic-finalization/v2"
        or receipt.get("analysis_domain_id") != analysis_domain_id
        or receipt.get("section_id") != section_id
        or receipt.get("tissue_generation_id") != tissue_generation_id
        or receipt.get("pipeline_run_id") != pipeline_run_id
        or receipt.get("semantic_result_set_sha256") != result_set_sha256
        or receipt.get("semantic_result_artifact") != artifact_mapping
        or isinstance(reported_active, (str, bytes))
        or not isinstance(reported_active, Sequence)
        or list(reported_active) != active
        or isinstance(accepted, (str, bytes))
        or not isinstance(accepted, Sequence)
        or list(accepted) != active
        or receipt.get("accepted_secondary_count") != len(active)
        or not _is_sha256(cell_digest)
        or type(cell_count) is not int or cell_count < 0
        or output.get("reconciled_cell_ids_sha256") != cell_digest
        or output.get("cell_count") != cell_count
        or result_artifact.record_count != len(authenticated_rows)
    ):
        raise ROIAnalysisError("semantic finalization receipt identity is invalid")
    converted = _json_value(receipt)
    assert isinstance(converted, dict)
    return converted


def evaluate_roi_semantic_result_set(
    *,
    profile_bindings: Sequence[BatchROISemanticProfileBinding],
    profile_bytes: Sequence[bytes],
    expected_validation_domain_id: str,
    configured_structure_ids: Sequence[str],
    evidence: Sequence[SemanticEvidenceSnapshot],
    decisions: Sequence[SemanticRegionDecision],
    raw_inventory: Mapping[str, Sequence[Mapping[str, object]]],
    slide_id: str,
    section_id: str,
    analysis_domain_id: str,
    tissue_generation_id: str,
    pipeline_run_id: str,
    review_status: str,
    semantic_config_sha256: str,
    action: str | None = None,
    offered_context: Mapping[str, object] | None = None,
    response: Mapping[str, object] | None = None,
) -> StageResult | dict[str, object]:
    """Evaluate one exact domain from authenticated profile bytes."""
    from .workflow import (
        next_semantic_region_interaction,
        validate_semantic_result_graph,
        _semantic_reviewer_evidence,
    )
    from mif_pipeline.roi_validation import evaluate_semantic_profile

    profiles = _profile_values(
        profile_bindings=profile_bindings,
        profile_bytes=profile_bytes,
        expected_validation_domain_id=expected_validation_domain_id,
    )
    configured = tuple(configured_structure_ids)
    if (
        not configured
        or any(type(item) is not str or not item for item in configured)
        or len(set(configured)) != len(configured)
        or {profile.structure_id for profile in profiles} != set(configured)
    ):
        raise ROIAnalysisError("semantic profiles do not cover every secondary structure")
    if review_status not in {"accepted", "rejected", "needs_review"}:
        raise ROIAnalysisError("semantic review status is invalid")
    if not _is_sha256(semantic_config_sha256):
        raise ROIAnalysisError("semantic configuration digest is invalid")
    snapshots = tuple(evidence)
    decision_values = list(decisions)
    if not all(isinstance(item, SemanticEvidenceSnapshot) for item in snapshots):
        raise ROIAnalysisError("semantic evidence is invalid")
    if not all(isinstance(item, SemanticRegionDecision) for item in decision_values):
        raise ROIAnalysisError("semantic region decision history is invalid")
    if any(
        (
            item.slide_id,
            item.section_id,
            item.analysis_domain_id,
        )
        != (slide_id, section_id, analysis_domain_id)
        for item in snapshots
    ):
        raise ROIAnalysisError("semantic evidence crosses finalization scope")
    profiles_by_structure = {item.structure_id: item for item in profiles}

    selected_candidate_roi_id = None
    if action in {
        "accept_semantic_region",
        "reject_semantic_region",
        "select_semantic_region",
    }:
        offered = offered_context if isinstance(offered_context, Mapping) else {}
        matching = [
            item
            for item in snapshots
            if item.analysis_domain_id == offered.get("analysis_domain_id")
            and item.candidate_roi_id == offered.get("roi_id")
            and item.structure_id == offered.get("structure_id")
        ]
        if len(matching) != 1:
            raise ROIAnalysisError("semantic interaction candidate identity is stale")
        else:
            snapshot = matching[0]
            profile = profiles_by_structure[snapshot.structure_id]
            exact = {
                "profile_id": profile.profile_id,
                "profile_version": profile.profile_version,
                "profile_content_sha256": profile.content_sha256,
                "evidence_snapshot_sha256": snapshot.digest,
                "geometry_sha256": snapshot.geometry_digest,
            }
            if any(offered.get(field) != value for field, value in exact.items()):
                raise ROIAnalysisError("semantic interaction authority is stale")
            else:
                checkpoint_id = offered.get("checkpoint_id")
                if type(checkpoint_id) is not str or not checkpoint_id:
                    raise ROIAnalysisError(
                        "semantic region decision lacks exact checkpoint identity"
                    )
                resolved_response = response if isinstance(response, Mapping) else {}
                try:
                    if action == "select_semantic_region":
                        if (profile.schema_version != "semantic-profile/v4"
                                or offered.get("schema_version") != "semantic-region-interaction/v2"
                                or set(resolved_response) != {"candidate_roi_id"}
                                or not isinstance(resolved_response["candidate_roi_id"], str)):
                            raise ValueError("choose an available region")
                        selected_candidate_roi_id = resolved_response["candidate_roi_id"]
                        decision = None
                    elif profile.schema_version == "semantic-profile/v4":
                        if offered.get("schema_version") != "semantic-region-interaction/v2":
                            raise ValueError("reopen the region review")
                        decision = snapshot.region_decision(
                            profile, checkpoint_id, str(resolved_response.get("reviewer_id", "")),
                            "accept" if action == "accept_semantic_region" else "reject",
                            subtype_status=resolved_response.get("subtype_status"),
                            semantic_class_id=resolved_response.get("semantic_class_id"),
                            rationale=resolved_response.get("rationale") or None,
                        )
                    else:
                        decision = snapshot.manual_decision(
                            profile.profile_id, profile.profile_version, profile.content_sha256,
                            str(offered.get("criterion_id")), checkpoint_id,
                            str(resolved_response.get("reviewer_id", "")),
                            "accept" if action == "accept_semantic_region" else "reject",
                            str(resolved_response.get("rationale", "")),
                        )
                    if decision is not None:
                        decision_values.append(decision)
                except (TypeError, ValueError) as exc:
                    raise ROIAnalysisError("semantic region decision response is invalid") from exc

    raw_secondary = raw_inventory.get("secondary")
    raw_primary = raw_inventory.get("primary")
    if (
        isinstance(raw_secondary, (str, bytes))
        or not isinstance(raw_secondary, Sequence)
        or isinstance(raw_primary, (str, bytes))
        or not isinstance(raw_primary, Sequence)
    ):
        raise ROIAnalysisError("semantic raw inventory is invalid")
    candidates = {item.get("roi_id"): item for item in raw_secondary if isinstance(item, Mapping)}
    if len(candidates) != len(raw_secondary) or set(candidates) != {
        item.candidate_roi_id for item in snapshots
    }:
        raise ROIAnalysisError("semantic evidence and raw candidate inventories differ")

    checkpoint_digest = _mapping_sha256(
        {
            "analysis_domain_id": analysis_domain_id,
            "evidence": [item.digest for item in snapshots],
            "profiles": [item.content_sha256 for item in profiles],
            "decisions": [item.to_mapping() for item in decision_values],
        }
    )
    interaction = next_semantic_region_interaction(
        profiles=profiles,
        evidence=snapshots,
        decisions=decision_values,
        tissue_generation_id=tissue_generation_id,
        checkpoint_id=f"semantic-region-{checkpoint_digest[:24]}",
        selected_candidate_roi_id=selected_candidate_roi_id,
    )
    if interaction is not None:
        if interaction.resume_context.get("schema_version") == "semantic-region-interaction/v2":
            candidate = candidates[interaction.resume_context["roi_id"]]
            interaction = replace(interaction, resume_context={
                **interaction.resume_context,
                "parent_roi_id": candidate["parent_roi_id"],
                "path_object_id": candidate["path_object_id"],
            })
        return interaction

    semantic_qc = {
        "status": "passed",
        "result_count": len(snapshots),
        "evaluated_roi_ids": sorted(str(item) for item in candidates),
        "semantic_config_sha256": semantic_config_sha256,
    }
    rows: list[dict[str, object]] = []
    for snapshot in snapshots:
        profile = profiles_by_structure.get(snapshot.structure_id)
        if profile is None:
            raise ROIAnalysisError("semantic evidence has no exact configured profile")
        evaluated = evaluate_semantic_profile(profile, snapshot, decision_values)
        current = profile.schema_version == "semantic-profile/v4"
        region_decision = None
        if current:
            matches = [item for item in decision_values if item.matches_region(profile, snapshot)]
            if not matches:
                raise ROIAnalysisError("region requires an explicit current decision")
            region_decision = matches[-1]
            semantic_status = "accepted" if region_decision.action == "accept" else "rejected"
            classification_status = region_decision.subtype_status
            semantic_class_id = region_decision.semantic_class_id
        else:
            if evaluated.root.outcome is RuleOutcome.NEEDS_REVIEW:
                raise ROIAnalysisError("semantic evaluation retained an unresolved manual criterion")
            classification_status = evaluated.classification_status.value
            if classification_status in {"needs_review", "ambiguous"}:
                raise ROIAnalysisError("semantic classification remains unresolved")
            semantic_status = {RuleOutcome.PASS: "accepted", RuleOutcome.FAIL: "rejected",
                               RuleOutcome.UNAVAILABLE: "indeterminate"}[evaluated.root.outcome]
            semantic_class_id = evaluated.semantic_class_id
        candidate = candidates[snapshot.candidate_roi_id]
        rows.append(
            {
                "schema_version": "semantic-result/v3" if current else "semantic-result/v2",
                **({"region_decision": region_decision.to_mapping(),
                    "measurement_results": _semantic_reviewer_evidence(profile, snapshot, decision_values)} if current else {}),
                "slide_id": slide_id,
                "section_id": section_id,
                "analysis_domain_id": analysis_domain_id,
                "tissue_generation_id": tissue_generation_id,
                "pipeline_run_id": pipeline_run_id,
                "roi_id": snapshot.candidate_roi_id,
                "path_object_id": candidate["path_object_id"],
                "structure_id": snapshot.structure_id,
                "parent_roi_id": candidate["parent_roi_id"],
                "semantic_profile": profile.identifier,
                "semantic_status": semantic_status,
                "review_status": semantic_status if current else review_status,
                "profile_id": profile.profile_id,
                "profile_version": profile.profile_version,
                "profile_content_sha256": profile.content_sha256,
                "evidence_snapshot_sha256": snapshot.digest,
                "geometry_sha256": snapshot.geometry_digest,
                "semantic_rule_results": _criterion_result_mapping(evaluated.root),
                "semantic_classification_status": classification_status,
                "semantic_class_id": semantic_class_id,
                "semantic_classification_rule_results": [
                    {
                        "class_id": item.class_id,
                        "result": _criterion_result_mapping(item.result),
                    }
                    for item in evaluated.classification_rule_results
                ],
            }
        )
    validated = list(
        validate_semantic_result_graph(
            rows,
            analysis_domain_id=analysis_domain_id,
            analysis_domain_parent_id=analysis_domain_id,
            accepted_primary_roi_ids=tuple(
                str(item["roi_id"]) for item in raw_primary if isinstance(item, Mapping)
            ),
            profiles=profiles,
        )
    )
    result_set_sha256 = _mapping_sha256(
        {
            "schema_version": "semantic-result-set/v1",
            "semantic_results": validated,
        }
    )
    return {
        "semantic_results": validated,
        "semantic_result_set_sha256": result_set_sha256,
        "semantic_qc": semantic_qc,
        "review_status": review_status,
        "semantic_region_decisions": [item.to_mapping() for item in decision_values],
    }


__all__ = [
    "authenticate_roi_semantic_result_artifact",
    "build_roi_semantic_manual_cap_interaction",
    "evaluate_roi_semantic_result_set",
    "prepare_roi_semantic_evidence_request",
    "prepare_roi_semantic_finalization_request",
    "publish_roi_semantic_result_artifact",
    "resolve_roi_semantic_count",
    "resolve_roi_terminal_semantic_review",
    "serialize_roi_semantic_result_artifact",
    "validate_roi_semantic_evidence_response",
    "validate_roi_semantic_finalization_response",
    "validate_roi_semantic_manual_cap_response",
    "validate_roi_semantic_raw_inventory",
    "validate_roi_terminal_semantic_review_response",
]
