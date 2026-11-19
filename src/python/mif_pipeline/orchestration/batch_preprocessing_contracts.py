"""Artifact identities and adapter specifications for early preprocessing.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from types import MappingProxyType

from mif_pipeline.preprocessing.inspection import ReviewedPanelMapping
from mif_pipeline.preprocessing.unmixing import FluorophoreReferenceLibrary

from .batch_artifacts import (
    ArtifactConfigurationBinding,
    ArtifactEquivalenceContract,
    ArtifactIdentityContract,
    ArtifactMultiplicity,
    ArtifactScope,
    DispatchMode,
    StageAdapterSpec,
    StageArtifactRequirement,
)
from .batch_dag import ResourceKind

_ZERO_SHA256 = "0" * 64
_TEMPLATE_BINDING = ArtifactConfigurationBinding("template", _ZERO_SHA256)
_TEMPLATE_SCOPE = ArtifactScope(None, None, None, None, ())

OUTPUT_BY_NODE = MappingProxyType(
    {
        "inspection": ("source_inspection", "mif-batch-source-inspection"),
        "channel_review": ("stage1_review", "mif-batch-stage1-review"),
        "reference_unmixing": (
            "processed_image",
            "mif-batch-reference-unmixing-completion",
        ),
    }
)
REQUIRED_INPUTS = MappingProxyType(
    {
        "inspection": (),
        "channel_review": ("source_inspection",),
        "reference_unmixing": ("source_inspection", "stage1_review"),
    }
)
RESOURCE_BY_NODE = MappingProxyType(
    {
        "inspection": ResourceKind.BIOFORMATS_CONVERSION.value,
        "channel_review": ResourceKind.SPECTRAL_PREPROCESSING.value,
        "reference_unmixing": ResourceKind.SPECTRAL_PREPROCESSING.value,
    }
)
OPTIONAL_INPUTS = MappingProxyType(
    {
        "inspection": (),
        "channel_review": ("reviewed_panel_mapping",),
        "reference_unmixing": ("spectral_reference_library",),
    }
)
SCHEMA_BY_ARTIFACT = MappingProxyType(
    {artifact_kind: schema for artifact_kind, schema in OUTPUT_BY_NODE.values()}
)


# ==============================================================================
#                         Batch preprocessing contracts
# ==============================================================================


def artifact_identity_fields(artifact_kind: str) -> tuple[str, ...]:
    common = (
        "equivalence.canonical_identity_sha256",
        "equivalence.stable_inventory_sha256",
        "equivalence.scientific_values_sha256",
        "equivalence.qc_evidence_sha256",
        "authority_sha256",
    )
    by_kind = {
        "source_inspection": (
            "source.file_sha256",
            "source.size_bytes",
            "source.modified_time_ns",
            "source.device_id",
            "source.inode_id",
            "selection.image_id",
            "selection.pixels_id",
            "selection.source_image_index",
            "selection.source_series_index",
            "selection.series_name",
            "ome_sha256",
            "series_inventory_sha256",
            "channel_inventory_sha256",
        ),
        "stage1_review": (
            "source_inspection_authority_sha256",
            "reviewed_ome_sha256",
            "channel_inventory_sha256",
            "component_inventory_sha256",
            "fluorophore_inventory_sha256",
            "biomarker_inventory_sha256",
            "replace_channel_names_with_biomarkers",
        ),
        "processed_image": (
            "source_inspection_artifact_ref_sha256",
            "stage1_review_artifact_ref_sha256",
            "target_image_sha256",
            "final_authority_id",
            "final_authority_sha256",
            "final_basis_sha256",
            "normalization_sha256",
            "image_generation_sha256",
            "publication.output_sha256",
            "publication.provenance_sha256",
            "publication.clipping_evidence_sha256",
            "operation_completion_sha256",
        ),
    }
    return tuple((*common, *by_kind[artifact_kind]))


def _template_value(field: str) -> object:
    if field.endswith(("_sha256", ".file_sha256")) or field in {
        "authority_sha256",
        "ome_sha256",
    }:
        return _ZERO_SHA256
    if field.endswith(("_bytes", "_ns", "_id")) and field not in {
        "selection.image_id",
        "selection.pixels_id",
        "final_authority_id",
    }:
        return 0
    if field == "replace_channel_names_with_biomarkers":
        return False
    if field.endswith("source_image_index") or field.endswith("source_series_index"):
        return 0
    return "template"


def nested_identity(fields: tuple[str, ...]) -> dict[str, object]:
    result: dict[str, object] = {}
    for path in fields:
        cursor = result
        parts = path.split(".")
        for part in parts[:-1]:
            child = cursor.setdefault(part, {})
            assert isinstance(child, dict)
            cursor = child
        cursor[parts[-1]] = _template_value(path)
    return result


def preprocessing_identity_contract(artifact_kind: str) -> ArtifactIdentityContract:
    version = 2 if artifact_kind == "processed_image" else 1
    return ArtifactIdentityContract(
        contract_id=f"mif-batch-{artifact_kind.replace('_', '-')}-identity/v{version}",
        artifact_kind=artifact_kind,
        schema_name=SCHEMA_BY_ARTIFACT[artifact_kind],
        schema_version=version,
        required_fields=artifact_identity_fields(artifact_kind),
        optional_fields=("operational.locator_sha256", "operational.timestamp"),
        required_runtime_names=("mif_pipeline",),
    )


def _input_template(artifact_kind: str) -> StageArtifactRequirement:
    contract = preprocessing_identity_contract(artifact_kind)
    return StageArtifactRequirement(
        artifact_kind=artifact_kind,
        schema_name=contract.schema_name,
        schema_version=contract.schema_version,
        scope=_TEMPLATE_SCOPE,
        owning_identity=nested_identity(contract.required_fields),
        identity_contract=contract,
        compatibility_signature_sha256=_ZERO_SHA256,
        producer_version="mif-pipeline-batch-preprocessing/v1",
        runtime_versions={"mif_pipeline": "0.1.0"},
        configuration_bindings=(_TEMPLATE_BINDING,),
        multiplicity=ArtifactMultiplicity.SINGLE,
    )


def reusable_input_identity_contract(artifact_kind: str) -> ArtifactIdentityContract:
    if artifact_kind not in {"reviewed_panel_mapping", "spectral_reference_library"}:
        raise ValueError("unsupported preprocessing reusable artifact kind")
    if artifact_kind == "reviewed_panel_mapping":
        schema_name = ReviewedPanelMapping.SCHEMA_NAME
        schema_version = ReviewedPanelMapping.SCHEMA_VERSION
        required_fields = (
            "mapping_id",
            "content_sha256",
            "review_id",
            "review_decision",
            "channel_selection_sha256",
            "component_inventory_sha256",
            "fluorophore_inventory_sha256",
            "biomarker_inventory_sha256",
            "replace_channel_names_with_biomarkers",
            "compatibility_signature_sha256",
            "producer_identity_sha256",
        )
        required_runtime_names = ("mif_pipeline",)
    else:
        schema_name = FluorophoreReferenceLibrary.SCHEMA_NAME
        schema_version = FluorophoreReferenceLibrary.SCHEMA_VERSION
        required_fields = (
            "artifact_id",
            "content_sha256",
            "axis_sha256",
            "component_order_sha256",
            "structural_signature_sha256",
            "quantitative_signature_sha256",
            "qc_sha256",
            "review_sha256",
            "software_versions_sha256",
            "policy_versions_sha256",
            "compatibility_signature_sha256",
        )
        required_runtime_names = ("pipeline",)
    return ArtifactIdentityContract(
        contract_id=f"mif-batch-{artifact_kind.replace('_', '-')}-owner-identity/v1",
        artifact_kind=artifact_kind,
        schema_name=schema_name,
        schema_version=schema_version,
        required_fields=required_fields,
        optional_fields=(),
        required_runtime_names=required_runtime_names,
    )


def _reusable_input_template(artifact_kind: str) -> StageArtifactRequirement:
    """Describe the current manifest-level reusable-artifact envelope."""

    contract = reusable_input_identity_contract(artifact_kind)
    return StageArtifactRequirement(
        artifact_kind=artifact_kind,
        schema_name=contract.schema_name,
        schema_version=contract.schema_version,
        scope=_TEMPLATE_SCOPE,
        owning_identity=nested_identity(contract.required_fields),
        identity_contract=contract,
        compatibility_signature_sha256=_ZERO_SHA256,
        producer_version="mif-pipeline-preprocessing-owner/v1",
        runtime_versions={name: "0.1.0" for name in contract.required_runtime_names},
        configuration_bindings=(_TEMPLATE_BINDING,),
        multiplicity=ArtifactMultiplicity.SINGLE,
    )


def build_preprocessing_stage_specs() -> tuple[StageAdapterSpec, ...]:
    """Return sample-neutral closed specs for the four real early DAG nodes."""

    specs: list[StageAdapterSpec] = []
    for node_kind, required_kinds in REQUIRED_INPUTS.items():
        version = "2" if node_kind == "reference_unmixing" else "1"
        output_kind, _schema = OUTPUT_BY_NODE[node_kind]
        output_contract = preprocessing_identity_contract(output_kind)
        paths = tuple(
            (output_kind, f"equivalence.{name}_sha256")
            for name in (
                "canonical_identity",
                "stable_inventory",
                "scientific_values",
                "qc_evidence",
            )
        )
        specs.append(
            StageAdapterSpec(
                node_kind=node_kind,
                node_version=version,
                required_inputs=tuple(_input_template(kind) for kind in required_kinds),
                optional_inputs=tuple(
                    _reusable_input_template(kind) for kind in OPTIONAL_INPUTS[node_kind]
                ),
                declared_outputs=(output_kind,),
                output_identity_contracts=(output_contract,),
                compatibility_signature_id=(
                    f"mif-batch-{node_kind.replace('_', '-')}-compatibility/v{version}"
                ),
                equivalence_contract=ArtifactEquivalenceContract(
                    contract_id=(f"mif-batch-{node_kind.replace('_', '-')}-equivalence/v{version}"),
                    byte_deterministic=False,
                    canonical_identity_paths=(paths[0],),
                    stable_inventory_paths=(paths[1],),
                    scientific_value_paths=(paths[2],),
                    qc_evidence_paths=(paths[3],),
                ),
                resource_kind=RESOURCE_BY_NODE[node_kind],
                dispatch_mode=DispatchMode.SINGLE,
            )
        )
    return tuple(sorted(specs, key=lambda item: item.node_kind))


__all__ = [
    "build_preprocessing_stage_specs",
    "preprocessing_identity_contract",
    "reusable_input_identity_contract",
]
