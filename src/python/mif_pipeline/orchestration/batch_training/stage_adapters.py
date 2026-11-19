"""Closed production adapter contracts for batch classifier training.

Training records keep subject roles, reference evidence, model settings and application
scope linked. Their validators and native adapters are called by the shared training service
before a fit, evaluation or accepted publication."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from ..batch_analysis_domain_artifacts import (
    accepted_analysis_domain_identity_contract,
)
from ..batch_analysis_preflight import analysis_preflight_identity_contract
from ..batch_artifacts import (
    ArtifactConfigurationBinding,
    ArtifactEquivalenceContract,
    ArtifactIdentityContract,
    ArtifactMultiplicity,
    ArtifactScope,
    DispatchMode,
    StageAdapterSpec,
    StageArtifactRequirement,
)
from ..batch_project import project_catalogue_entry_identity_contract
from ..batch_measurement_artifacts import measurements_result_identity_contract
from ..batch_phenotyping_mode_artifacts import phenotyping_mode_identity_contract
from ..batch_classifier_inference_contracts import (
    primary_classifier_inference_result_identity_contract,
)
from .planning import training_plan_set_identity_contract

primary_inference_result_identity_contract = primary_classifier_inference_result_identity_contract


TRAINING_PLAN_REVIEW_NODE_KINDS = (
    "primary_classifier_training_plan_review",
    "secondary_classifier_training_plan_review",
    "supervised_phenotype_training_plan_review",
)
TRAINING_EVIDENCE_NODE_KINDS = (
    "primary_training_evidence_snapshot",
    "secondary_training_evidence_snapshot",
    "supervised_training_evidence_snapshot",
)
TRAINING_PARENT_NODE_KINDS = (
    "primary_classifier_training_review",
    "secondary_classifier_training_review",
    "supervised_phenotype_training_review",
)
TRAINING_NODE_KINDS = (
    *TRAINING_PLAN_REVIEW_NODE_KINDS,
    *TRAINING_EVIDENCE_NODE_KINDS,
    *TRAINING_PARENT_NODE_KINDS,
)

_ZERO_SHA256 = "0" * 64
_TEMPLATE_SCOPE = ArtifactScope(None, None, None, None, ())
_TEMPLATE_BINDING = ArtifactConfigurationBinding("template", _ZERO_SHA256)

_EVIDENCE_FIELDS = (
    "analysis_domain_binding_set_sha256",
    "entry_snapshot_set_sha256",
    "entry_state_sha256",
    "object_output_count",
    "object_output_sha256",
    "plan_role_set_sha256",
    "post_binding_saved_state_sha256",
    "project_entry_id",
    "sample_id",
    "saved_state_after_sha256",
    "saved_state_before_sha256",
    "slide_id",
    "training_plan_set_sha256",
)
_PIXEL_PLAN_EVIDENCE_FIELDS = (
    "plan_evidence_commitment_sha256s",
    "training_plan_ids",
)
_CLASSIFIER_RECORD_SET_FIELDS = (
    "classifier_kind",
    "classifier_record_set_sha256",
    "evidence_child_inventory_sha256",
    "material_evidence_sha256",
    "output_inventory",
    "source_training_plan_set_sha256",
    "validation_domain_id",
)


# ==============================================================================
#                                 Stage adapters
# ==============================================================================


def _identity_contract(
    artifact_kind: str,
    schema_name: str,
    fields: tuple[str, ...],
) -> ArtifactIdentityContract:
    return ArtifactIdentityContract(
        contract_id=f"mif-batch-{artifact_kind.replace('_', '-')}-identity/v1",
        artifact_kind=artifact_kind,
        schema_name=schema_name,
        schema_version=1,
        required_fields=fields,
        optional_fields=(),
        required_runtime_names=("mif_pipeline",),
    )


def training_evidence_identity_contract(
    artifact_kind: str,
) -> ArtifactIdentityContract:
    if artifact_kind not in {
        "primary_training_evidence_snapshot",
        "secondary_training_evidence_snapshot",
        "supervised_training_evidence_snapshot",
    }:
        raise ValueError("artifact_kind is not a Task 11 evidence kind")
    return _identity_contract(
        artifact_kind,
        "roi-generation-manifest",
        (
            _EVIDENCE_FIELDS
            if artifact_kind == "supervised_training_evidence_snapshot"
            else (*_EVIDENCE_FIELDS, *_PIXEL_PLAN_EVIDENCE_FIELDS)
        ),
    )


def classifier_record_set_identity_contract() -> ArtifactIdentityContract:
    return _identity_contract(
        "classifier_record_set",
        "mif-classifier-record-set",
        _CLASSIFIER_RECORD_SET_FIELDS,
    )


def _template_value(field: str) -> object:
    if field.endswith("_sha256"):
        return _ZERO_SHA256
    if field.endswith("_count"):
        return 0
    if field == "output_inventory":
        return ()
    return "template"


def _identity_template(contract: ArtifactIdentityContract) -> dict[str, object]:
    result: dict[str, object] = {}
    for field in contract.required_fields:
        cursor = result
        parts = field.split(".")
        for part in parts[:-1]:
            child = cursor.setdefault(part, {})
            assert isinstance(child, dict)
            cursor = child
        cursor[parts[-1]] = _template_value(field)
    return result


def _requirement(
    contract: ArtifactIdentityContract,
    multiplicity: ArtifactMultiplicity,
) -> StageArtifactRequirement:
    return StageArtifactRequirement(
        artifact_kind=contract.artifact_kind,
        schema_name=contract.schema_name,
        schema_version=contract.schema_version,
        scope=_TEMPLATE_SCOPE,
        owning_identity=_identity_template(contract),
        identity_contract=contract,
        compatibility_signature_sha256=_ZERO_SHA256,
        producer_version="template",
        runtime_versions={name: "template" for name in contract.required_runtime_names},
        configuration_bindings=(_TEMPLATE_BINDING,),
        multiplicity=multiplicity,
    )


def _spec(
    node_kind: str,
    required_inputs: tuple[StageArtifactRequirement, ...],
    output: ArtifactIdentityContract,
    *,
    resource_kind: str,
    dispatch_mode: DispatchMode,
    optional_inputs: tuple[StageArtifactRequirement, ...] = (),
) -> StageAdapterSpec:
    return StageAdapterSpec(
        node_kind=node_kind,
        node_version="2" if node_kind in TRAINING_PLAN_REVIEW_NODE_KINDS else "1",
        required_inputs=tuple(sorted(required_inputs, key=lambda item: item.artifact_kind)),
        optional_inputs=optional_inputs,
        declared_outputs=(output.artifact_kind,),
        output_identity_contracts=(output,),
        compatibility_signature_id=f"{node_kind}-compatibility/v1",
        equivalence_contract=ArtifactEquivalenceContract(
            contract_id=f"{node_kind}-equivalence/v1",
            byte_deterministic=True,
            canonical_identity_paths=(),
            stable_inventory_paths=(),
            scientific_value_paths=(),
            qc_evidence_paths=(),
        ),
        resource_kind=resource_kind,
        dispatch_mode=dispatch_mode,
    )


def build_training_stage_adapters() -> tuple[StageAdapterSpec, ...]:
    """Return the exact closed nine-node adapter registry."""

    accepted = accepted_analysis_domain_identity_contract()
    frame = analysis_preflight_identity_contract()
    catalogue = project_catalogue_entry_identity_contract()
    measurements = measurements_result_identity_contract()
    mode = phenotyping_mode_identity_contract()
    plan_set = training_plan_set_identity_contract()
    primary_inference = primary_inference_result_identity_contract()
    record_set = classifier_record_set_identity_contract()
    evidence = {
        kind: training_evidence_identity_contract(kind)
        for kind in (
            "primary_training_evidence_snapshot",
            "secondary_training_evidence_snapshot",
            "supervised_training_evidence_snapshot",
        )
    }

    plan_sources = (
        _requirement(accepted, ArtifactMultiplicity.PER_PARTICIPANT),
        _requirement(frame, ArtifactMultiplicity.PER_PARTICIPANT),
        _requirement(catalogue, ArtifactMultiplicity.PER_PARTICIPANT),
    )
    supervised_plan_sources = plan_sources + (
        _requirement(measurements, ArtifactMultiplicity.EXACT_SET),
        _requirement(mode, ArtifactMultiplicity.EXACT_SET),
    )
    specs = (
        _spec(
            "primary_classifier_training_plan_review",
            plan_sources,
            plan_set,
            resource_kind="shared_classifier_mutation",
            dispatch_mode=DispatchMode.SINGLE,
        ),
        _spec(
            "secondary_classifier_training_plan_review",
            plan_sources,
            plan_set,
            resource_kind="shared_classifier_mutation",
            dispatch_mode=DispatchMode.SINGLE,
        ),
        _spec(
            "supervised_phenotype_training_plan_review",
            supervised_plan_sources,
            plan_set,
            resource_kind="shared_classifier_mutation",
            dispatch_mode=DispatchMode.SINGLE,
        ),
        _spec(
            "primary_training_evidence_snapshot",
            (
                _requirement(accepted, ArtifactMultiplicity.SINGLE),
                _requirement(plan_set, ArtifactMultiplicity.SINGLE),
            ),
            evidence["primary_training_evidence_snapshot"],
            resource_kind="qupath_image_read",
            dispatch_mode=DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE,
        ),
        _spec(
            "secondary_training_evidence_snapshot",
            (
                _requirement(accepted, ArtifactMultiplicity.SINGLE),
                _requirement(plan_set, ArtifactMultiplicity.EXACT_SET),
            ),
            evidence["secondary_training_evidence_snapshot"],
            resource_kind="qupath_image_read",
            dispatch_mode=DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE,
            optional_inputs=(_requirement(primary_inference, ArtifactMultiplicity.EXACT_SET),),
        ),
        _spec(
            "supervised_training_evidence_snapshot",
            (
                _requirement(accepted, ArtifactMultiplicity.SINGLE),
                _requirement(measurements, ArtifactMultiplicity.EXACT_SET),
                _requirement(mode, ArtifactMultiplicity.EXACT_SET),
                _requirement(plan_set, ArtifactMultiplicity.SINGLE),
            ),
            evidence["supervised_training_evidence_snapshot"],
            resource_kind="qupath_image_read",
            dispatch_mode=DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE,
        ),
        _spec(
            "primary_classifier_training_review",
            (
                _requirement(
                    evidence["primary_training_evidence_snapshot"],
                    ArtifactMultiplicity.EXACT_SET,
                ),
                _requirement(plan_set, ArtifactMultiplicity.SINGLE),
            ),
            record_set,
            resource_kind="shared_classifier_mutation",
            dispatch_mode=DispatchMode.SINGLE,
        ),
        _spec(
            "secondary_classifier_training_review",
            (
                _requirement(
                    evidence["secondary_training_evidence_snapshot"],
                    ArtifactMultiplicity.EXACT_SET,
                ),
                _requirement(plan_set, ArtifactMultiplicity.SINGLE),
            ),
            record_set,
            resource_kind="shared_classifier_mutation",
            dispatch_mode=DispatchMode.SINGLE,
        ),
        _spec(
            "supervised_phenotype_training_review",
            (
                _requirement(
                    evidence["supervised_training_evidence_snapshot"],
                    ArtifactMultiplicity.EXACT_SET,
                ),
                _requirement(plan_set, ArtifactMultiplicity.SINGLE),
            ),
            record_set,
            resource_kind="shared_classifier_mutation",
            dispatch_mode=DispatchMode.SINGLE,
        ),
    )
    by_kind = {spec.node_kind: spec for spec in specs}
    return tuple(by_kind[kind] for kind in TRAINING_NODE_KINDS)


__all__ = [
    "TRAINING_EVIDENCE_NODE_KINDS",
    "TRAINING_NODE_KINDS",
    "TRAINING_PARENT_NODE_KINDS",
    "TRAINING_PLAN_REVIEW_NODE_KINDS",
    "build_training_stage_adapters",
    "classifier_record_set_identity_contract",
    "primary_inference_result_identity_contract",
    "training_evidence_identity_contract",
]
