"""Frozen phenotyping stage inventory and owner routes.

This module declares ownership only. It deliberately does not retain runtime state or
instantiate scheduler adapters.

The `PhenotypingStageOwnerBinding` record carries `spec`, `direct_owners`,
`project_codec_factory`, `response_validator`.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from mif_pipeline.orchestration.batch_analysis_domain_artifacts import (
    accepted_analysis_domain_identity_contract,
)
from mif_pipeline.orchestration.batch_analysis_preflight import (
    analysis_preflight_identity_contract,
)
from mif_pipeline.orchestration.batch_artifacts import (
    ArtifactConfigurationBinding,
    ArtifactEquivalenceContract,
    ArtifactIdentityContract,
    ArtifactMultiplicity,
    ArtifactScope,
    BatchArtifactError,
    DispatchMode,
    StageAdapterSpec,
    StageArtifactRequirement,
)
from mif_pipeline.orchestration.batch_classifier_inference_artifacts import (
    primary_classifier_inference_result_identity_contract,
    secondary_classifier_inference_result_identity_contract,
)
from mif_pipeline.orchestration.batch_measurement_artifacts import (
    measurements_result_identity_contract,
)
from mif_pipeline.orchestration.batch_phenotyping_mode_artifacts import (
    build_phenotyping_mode_execution_result,
    phenotyping_mode_identity_contract,
)
from mif_pipeline.orchestration.batch_phenotyping_contracts import (
    semantic_profile_identity_contract,
)
from mif_pipeline.orchestration.batch_project import (
    project_catalogue_entry_identity_contract,
)
from mif_pipeline.orchestration.batch_segmentation_artifacts import (
    segmentation_result_identity_contract,
)
from mif_pipeline.orchestration.batch_stage_adapters import (
    execute_no_secondary_completion_stage,
)
from mif_pipeline.orchestration.batch_tissue_artifacts import (
    tissue_acceptance_result_identity_contract,
)
from mif_pipeline.orchestration.batch_training import (
    classifier_record_set_identity_contract,
)
from mif_pipeline.orchestration.project_stage_codecs import ProjectStageCodec

from .application_adapters import (
    build_cluster_label_import_project_stage_codec,
    build_phenotype_hierarchy_attestation_execution_result,
)
from .application_codec import cluster_label_import_result_identity_contract
from .application_records import (
    phenotype_hierarchy_attestation_identity_contract,
    semantic_raw_inventory_identity_contract,
)
from .authority import (
    clustering_result_identity_contract,
    unsupervised_preparation_identity_contract,
)
from .codec import persist_batch_clustering_result
from .execution import execute_batch_clustering
from .export_execution import (
    build_final_non_spatial_export_stage_spec,
    execute_final_non_spatial_export_stage,
)
from .handoff_execution import (
    build_phenotype_handoff_project_stage_codec,
    build_phenotype_handoff_stage_spec,
)
from .marker_application_adapters import build_marker_rule_application_project_stage_codec
from .marker_application_records import marker_section_application_identity_contract
from .marker_execution import (
    build_marker_gate_evidence,
    build_marker_gate_execution_result,
    marker_gate_domain_identity_contract,
    validate_marker_gate_target_response,
)
from .preparation import build_unsupervised_preparation_execution_result
from .review_authority import (
    accept_cluster_label_review,
    accepted_cluster_label_review_identity_contract,
    cluster_review_evidence_identity_contract,
)
from .review_execution import build_batch_cluster_review_evidence
from .semantic_adapters import (
    build_semantic_evidence_stage_codec,
    build_semantic_finalization_stage_codec,
    execute_final_count_cap_stage,
    execute_semantic_evaluation_stage,
    validate_final_count_cap_target_response,
    validate_semantic_evaluation_target_response,
)
from .semantic_applicable import (
    final_count_cap_result_identity_contract,
    semantic_evaluation_result_identity_contract,
    semantic_evidence_result_identity_contract,
    semantic_finalization_result_identity_contract,
)
from .semantic_completion import no_secondary_completion_identity_contract
from .semantic_raw_producer import build_semantic_raw_inventory_project_stage_codec
from .supervised_adapters import (
    build_supervised_phenotype_project_stage_codec,
    build_supervised_phenotype_stage_spec,
)
from .supervised_records import supervised_phenotype_result_identity_contract
from .supervised_execution import validate_supervised_prediction_target_response

from .marker_combinations import (marker_combination_identity_contract, execute_marker_combination_selection, validate_combination_target_response)

PHENOTYPING_STAGE_NODE_KINDS = (
    "phenotyping_mode_resolution",
    "marker_gate_review",
    "marker_combination_selection",
    "marker_rule_application",
    "supervised_phenotype_inference",
    "unsupervised_preparation",
    "clustering_per_slide",
    "clustering_pooled",
    "cluster_review_evidence",
    "cluster_label_review",
    "clustering_label_import",
    "semantic_raw_inventory_snapshot",
    "phenotype_hierarchy_attestation",
    "no_secondary_completion",
    "semantic_evidence",
    "semantic_evaluation",
    "final_count_cap",
    "semantic_finalization",
    "phenotype_handoff",
    "final_non_spatial_export",
)

PROJECT_NODE_KINDS = frozenset(
    {
        "marker_rule_application",
        "supervised_phenotype_inference",
        "clustering_label_import",
        "semantic_raw_inventory_snapshot",
        "semantic_evidence",
        "semantic_finalization",
        "phenotype_handoff",
    }
)

_ZERO_SHA256 = "0" * 64
_TEMPLATE_SCOPE = ArtifactScope(
    "template-domain", "template-sample", "template-slide", "template-entry", ()
)
_TEMPLATE_BINDING = (ArtifactConfigurationBinding("template-sample", _ZERO_SHA256),)


# ==============================================================================
#                                    Adapters
# ==============================================================================


@dataclass(frozen=True, slots=True)
class PhenotypingStageOwnerBinding:
    spec: StageAdapterSpec
    direct_owners: tuple[Callable[..., object], ...] = ()
    project_codec_factory: Callable[..., ProjectStageCodec] | None = None
    response_validator: Callable[..., object] | None = None

    def __post_init__(self) -> None:
        if type(self.spec) is not StageAdapterSpec:
            raise BatchArtifactError("phenotyping binding requires an exact spec")
        if not all(callable(item) for item in self.direct_owners):
            raise BatchArtifactError("direct owner bindings must be callable")
        if bool(self.direct_owners) == (self.project_codec_factory is not None):
            raise BatchArtifactError("phenotyping binding requires exactly one owner route")
        if self.project_codec_factory is not None and not callable(self.project_codec_factory):
            raise BatchArtifactError("project codec factory must be callable")
        if self.response_validator is not None and not callable(self.response_validator):
            raise BatchArtifactError("response validator must be callable")


def _requirement(
    contract: ArtifactIdentityContract, multiplicity: ArtifactMultiplicity
) -> StageArtifactRequirement:
    return StageArtifactRequirement(
        artifact_kind=contract.artifact_kind,
        schema_name=contract.schema_name,
        schema_version=contract.schema_version,
        scope=_TEMPLATE_SCOPE,
        owning_identity=_nested_identity(contract.required_fields),
        identity_contract=contract,
        compatibility_signature_sha256=_ZERO_SHA256,
        producer_version="mif-pipeline-task13-owner/v1",
        runtime_versions={name: "template" for name in contract.required_runtime_names},
        configuration_bindings=_TEMPLATE_BINDING,
        multiplicity=multiplicity,
    )


def _nested_identity(fields: tuple[str, ...]) -> dict[str, object]:
    value: dict[str, object] = {}
    for path in fields:
        cursor = value
        for part in path.split(".")[:-1]:
            cursor = cursor.setdefault(part, {})  # type: ignore[assignment]
        cursor[path.split(".")[-1]] = "template"
    return value


def _spec(
    node_kind: str,
    output: ArtifactIdentityContract,
    required: Mapping[ArtifactIdentityContract, ArtifactMultiplicity],
    optional: Mapping[ArtifactIdentityContract, ArtifactMultiplicity] | None = None,
) -> StageAdapterSpec:
    project = node_kind in PROJECT_NODE_KINDS
    return StageAdapterSpec(
        node_kind=node_kind,
        node_version="1",
        required_inputs=tuple(
            _requirement(contract, count) for contract, count in required.items()
        ),
        optional_inputs=tuple(
            _requirement(contract, count) for contract, count in (optional or {}).items()
        ),
        declared_outputs=(output.artifact_kind,),
        output_identity_contracts=(output,),
        compatibility_signature_id=f"mif-batch-{node_kind}-compatibility/v1",
        equivalence_contract=ArtifactEquivalenceContract(
            contract_id=f"mif-batch-{node_kind}-equivalence/v1",
            byte_deterministic=True,
            canonical_identity_paths=(),
            stable_inventory_paths=(),
            scientific_value_paths=(),
            qc_evidence_paths=(),
        ),
        resource_kind="qupath_image_write" if project else "scientific_compute",
        dispatch_mode=(
            DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE if project else DispatchMode.SINGLE
        ),
    )


def build_phenotyping_stage_specs() -> tuple[StageAdapterSpec, ...]:
    """Return the closed, independently addressable stage inventory."""
    frame = analysis_preflight_identity_contract()
    domains = accepted_analysis_domain_identity_contract()
    measurements = measurements_result_identity_contract()
    modes = phenotyping_mode_identity_contract()
    segmentation = segmentation_result_identity_contract()
    tissue = tissue_acceptance_result_identity_contract()
    projects = project_catalogue_entry_identity_contract()
    classifiers = classifier_record_set_identity_contract()
    marker_gate = marker_gate_domain_identity_contract()
    combinations = marker_combination_identity_contract()
    marker_result = marker_section_application_identity_contract()
    preparation = unsupervised_preparation_identity_contract()
    clustering = clustering_result_identity_contract()
    review_evidence = cluster_review_evidence_identity_contract()
    label_review = accepted_cluster_label_review_identity_contract()
    label_import = cluster_label_import_result_identity_contract()
    raw = semantic_raw_inventory_identity_contract()
    hierarchy = phenotype_hierarchy_attestation_identity_contract()
    no_secondary = no_secondary_completion_identity_contract()
    evidence = semantic_evidence_result_identity_contract()
    evaluation = semantic_evaluation_result_identity_contract()
    capped = final_count_cap_result_identity_contract()
    finalization = semantic_finalization_result_identity_contract()
    from ..batch_training.planning import training_plan_set_identity_contract
    primary_choice = training_plan_set_identity_contract()
    primary = primary_classifier_inference_result_identity_contract()
    secondary = secondary_classifier_inference_result_identity_contract()
    supervised = supervised_phenotype_result_identity_contract()
    semantic_profile = semantic_profile_identity_contract()

    direct = {
        "phenotyping_mode_resolution": _spec(
            "phenotyping_mode_resolution", modes, {measurements: ArtifactMultiplicity.SINGLE}
        ),
        "marker_gate_review": _spec(
            "marker_gate_review",
            marker_gate,
            {measurements: ArtifactMultiplicity.EXACT_SET, modes: ArtifactMultiplicity.EXACT_SET},
        ),
        "marker_combination_selection": _spec("marker_combination_selection", combinations, {marker_gate: ArtifactMultiplicity.SINGLE}),
        "marker_rule_application": _spec(
            "marker_rule_application",
            marker_result,
            {
                marker_gate: ArtifactMultiplicity.SINGLE,
                combinations: ArtifactMultiplicity.SINGLE,
                measurements: ArtifactMultiplicity.SINGLE,
                modes: ArtifactMultiplicity.EXACT_SET,
            },
        ),
        "unsupervised_preparation": _spec(
            "unsupervised_preparation",
            preparation,
            {measurements: ArtifactMultiplicity.SINGLE, modes: ArtifactMultiplicity.EXACT_SET},
        ),
        "clustering_per_slide": _spec(
            "clustering_per_slide",
            clustering,
            {
                measurements: ArtifactMultiplicity.EXACT_SET,
                modes: ArtifactMultiplicity.EXACT_SET,
                preparation: ArtifactMultiplicity.EXACT_SET,
            },
        ),
        "clustering_pooled": _spec(
            "clustering_pooled",
            clustering,
            {
                measurements: ArtifactMultiplicity.EXACT_SET,
                modes: ArtifactMultiplicity.EXACT_SET,
                preparation: ArtifactMultiplicity.EXACT_SET,
            },
        ),
        "cluster_review_evidence": _spec(
            "cluster_review_evidence",
            review_evidence,
            {
                clustering: ArtifactMultiplicity.SINGLE,
                measurements: ArtifactMultiplicity.EXACT_SET,
                preparation: ArtifactMultiplicity.EXACT_SET,
            },
        ),
        "cluster_label_review": _spec(
            "cluster_label_review", label_review, {review_evidence: ArtifactMultiplicity.SINGLE}
        ),
        "clustering_label_import": _spec(
            "clustering_label_import",
            label_import,
            {
                domains: ArtifactMultiplicity.EXACT_SET,
                label_review: ArtifactMultiplicity.SINGLE,
                clustering: ArtifactMultiplicity.SINGLE,
                measurements: ArtifactMultiplicity.SINGLE,
                modes: ArtifactMultiplicity.EXACT_SET,
                segmentation: ArtifactMultiplicity.SINGLE,
            },
        ),
        "semantic_raw_inventory_snapshot": _spec(
            "semantic_raw_inventory_snapshot",
            raw,
            {
                frame: ArtifactMultiplicity.SINGLE,
                tissue: ArtifactMultiplicity.SINGLE,
                domains: ArtifactMultiplicity.SINGLE,
                segmentation: ArtifactMultiplicity.SINGLE,
                measurements: ArtifactMultiplicity.SINGLE,
            },
            {
                primary: ArtifactMultiplicity.SINGLE,
                primary_choice: ArtifactMultiplicity.SINGLE,
                secondary: ArtifactMultiplicity.EXACT_SET,
                label_import: ArtifactMultiplicity.SINGLE,
                marker_result: ArtifactMultiplicity.SINGLE,
                supervised: ArtifactMultiplicity.SINGLE,
            },
        ),
        "phenotype_hierarchy_attestation": _spec(
            "phenotype_hierarchy_attestation",
            hierarchy,
            {label_import: ArtifactMultiplicity.SINGLE, raw: ArtifactMultiplicity.SINGLE},
        ),
        "no_secondary_completion": _spec(
            "no_secondary_completion",
            no_secondary,
            {
                frame: ArtifactMultiplicity.SINGLE,
                domains: ArtifactMultiplicity.SINGLE,
                measurements: ArtifactMultiplicity.SINGLE,
                raw: ArtifactMultiplicity.SINGLE,
            },
            {
                marker_result: ArtifactMultiplicity.SINGLE,
                label_import: ArtifactMultiplicity.SINGLE,
                supervised: ArtifactMultiplicity.SINGLE,
                hierarchy: ArtifactMultiplicity.SINGLE,
            },
        ),
        "semantic_evaluation": _spec(
            "semantic_evaluation",
            evaluation,
            {
                evidence: ArtifactMultiplicity.SINGLE,
                raw: ArtifactMultiplicity.SINGLE,
                semantic_profile: ArtifactMultiplicity.EXACT_SET,
            },
        ),
        "final_count_cap": _spec(
            "final_count_cap",
            capped,
            {
                evaluation: ArtifactMultiplicity.SINGLE,
                raw: ArtifactMultiplicity.SINGLE,
                domains: ArtifactMultiplicity.SINGLE,
            },
        ),
    }
    semantic_evidence = _spec(
        "semantic_evidence",
        evidence,
        {
            frame: ArtifactMultiplicity.SINGLE,
            domains: ArtifactMultiplicity.SINGLE,
            measurements: ArtifactMultiplicity.SINGLE,
            raw: ArtifactMultiplicity.SINGLE,
            semantic_profile: ArtifactMultiplicity.EXACT_SET,
        },
        {
            marker_result: ArtifactMultiplicity.SINGLE,
            supervised: ArtifactMultiplicity.SINGLE,
            label_import: ArtifactMultiplicity.SINGLE,
            hierarchy: ArtifactMultiplicity.SINGLE,
        },
    )
    semantic_finalization = _spec(
        "semantic_finalization",
        finalization,
        {
            evaluation: ArtifactMultiplicity.SINGLE,
            capped: ArtifactMultiplicity.SINGLE,
            raw: ArtifactMultiplicity.SINGLE,
            frame: ArtifactMultiplicity.SINGLE,
            domains: ArtifactMultiplicity.SINGLE,
            measurements: ArtifactMultiplicity.SINGLE,
        },
    )
    direct.update(
        {"semantic_evidence": semantic_evidence, "semantic_finalization": semantic_finalization}
    )
    direct["supervised_phenotype_inference"] = build_supervised_phenotype_stage_spec()
    direct["phenotype_handoff"] = build_phenotype_handoff_stage_spec()
    direct["final_non_spatial_export"] = build_final_non_spatial_export_stage_spec()
    return tuple(direct[kind] for kind in PHENOTYPING_STAGE_NODE_KINDS)


_PROJECT_CODEC_FACTORIES = MappingProxyType(
    {
        "marker_rule_application": build_marker_rule_application_project_stage_codec,
        "supervised_phenotype_inference": build_supervised_phenotype_project_stage_codec,
        "clustering_label_import": build_cluster_label_import_project_stage_codec,
        "semantic_raw_inventory_snapshot": build_semantic_raw_inventory_project_stage_codec,
        "semantic_evidence": build_semantic_evidence_stage_codec,
        "semantic_finalization": build_semantic_finalization_stage_codec,
        "phenotype_handoff": build_phenotype_handoff_project_stage_codec,
    }
)
_DIRECT_OWNERS = MappingProxyType(
    {
        "phenotyping_mode_resolution": (build_phenotyping_mode_execution_result,),
        "marker_gate_review": (build_marker_gate_evidence, build_marker_gate_execution_result),
        "marker_combination_selection": (execute_marker_combination_selection,),
        "unsupervised_preparation": (build_unsupervised_preparation_execution_result,),
        "clustering_per_slide": (execute_batch_clustering, persist_batch_clustering_result),
        "clustering_pooled": (execute_batch_clustering, persist_batch_clustering_result),
        "cluster_review_evidence": (build_batch_cluster_review_evidence,),
        "cluster_label_review": (accept_cluster_label_review,),
        "phenotype_hierarchy_attestation": (
            build_phenotype_hierarchy_attestation_execution_result,
        ),
        "no_secondary_completion": (execute_no_secondary_completion_stage,),
        "semantic_evaluation": (execute_semantic_evaluation_stage,),
        "final_count_cap": (execute_final_count_cap_stage,),
        "final_non_spatial_export": (execute_final_non_spatial_export_stage,),
    }
)
_RESPONSE_VALIDATORS = {
    "marker_gate_review": validate_marker_gate_target_response,
    "marker_combination_selection": validate_combination_target_response,
    "supervised_phenotype_inference": validate_supervised_prediction_target_response,
    "semantic_evaluation": validate_semantic_evaluation_target_response,
    "final_count_cap": validate_final_count_cap_target_response,
}


def phenotyping_response_validator(node_kind: str) -> Callable[..., object] | None:
    """Return a stage's response validator without constructing its execution spec."""
    if node_kind not in PHENOTYPING_STAGE_NODE_KINDS:
        raise BatchArtifactError("phenotyping response validator stage is unsupported")
    validator = _RESPONSE_VALIDATORS.get(node_kind)
    if validator is not None and not callable(validator):
        raise BatchArtifactError("response validator must be callable")
    return validator


def build_phenotyping_stage_owner_bindings() -> tuple[PhenotypingStageOwnerBinding, ...]:
    """Bind every frozen spec to exactly one accepted owner route."""
    return tuple(
        PhenotypingStageOwnerBinding(
            spec=spec,
            direct_owners=_DIRECT_OWNERS.get(spec.node_kind, ()),
            project_codec_factory=_PROJECT_CODEC_FACTORIES.get(spec.node_kind),
            response_validator=phenotyping_response_validator(spec.node_kind),
        )
        for spec in build_phenotyping_stage_specs()
    )


__all__ = [
    "PHENOTYPING_STAGE_NODE_KINDS",
    "PROJECT_NODE_KINDS",
    "PhenotypingStageOwnerBinding",
    "build_phenotyping_stage_owner_bindings",
    "build_phenotyping_stage_specs",
    "phenotyping_response_validator",
]
