"""Composite-safe records for reviewed cluster-label application.

The `ClusterLabelSectionApplication` record carries `sample_id`, `project_entry_id`,
`slide_id`, `canonical_image_id`, `canonical_image_sha256` and its remaining declared
fields.

The operation retains the selected stable-cell population, measurement generation and
phenotype view. Its records and stage adapters connect accepted numerical results to the
current batch target and reviewed application scope."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from mif_pipeline.pipeline_core import AnalysisDomainKind
from mif_pipeline.orchestration.batch_artifacts import (
    ArtifactIdentityContract,
    BatchArtifactRef,
)

from ._common import (
    BatchPhenotypingError,
    canonical_bytes,
    digest,
    exact_tuple,
    sha256,
    text,
)

# Native semantic consumers reopen these accepted annotation and dataset inputs
# from the existing SHA-bound raw snapshot; cells are never included here.
SEMANTIC_LIVE_BINDING_FIELDS = frozenset({
    "run_id", "generation_id", "canonical_image_id", "canonical_image_sha256",
    "section_id", "analysis_domain_id", "analysis_domain_revision",
    "analysis_domain_revision_sha256", "analysis_domain_path_object_id",
    "domain_set_sha256", "analysis_domain_selection_status",
    "analysis_domain_manifests", "accepted_tissue_section_authorities",
    "analysis_domain_revision_evidence", "semantic_roi_sources", "semantic_cell_dataset",
    "semantic_cell_count", "semantic_cell_ids_sha256", "segmentation_population_id",
})

PHENOTYPE_HIERARCHY_ATTESTATION_SCHEMA = "mif-batch-phenotype-view-attestation/v1"

_SEMANTIC_RAW_INVENTORY_IDENTITY_FIELDS = tuple(
    sorted(
        {
            "sample_id",
            "project_entry_id",
            "slide_id",
            "canonical_image_id",
            "canonical_image_sha256",
            "section_id",
            "tissue_generation_id",
            "segmentation_population_id",
            "phenotype_generation_id",
            "phenotype_source_artifact_ref_sha256",
            "raw_export_sha256",
            "raw_export_byte_count",
            "saved_state_after_sha256",
            "cell_ids_sha256",
            "cell_to_domain_membership_sha256",
            "configured_secondary_ids",
            "configured_secondary_ids_sha256",
            "candidate_secondary_roi_ids",
            "candidate_secondary_roi_ids_sha256",
            "primary_domain_status",
            "execution_exchange_sha256",
        }
    )
)

_ATTESTATION_IDENTITY_FIELDS = tuple(
    sorted(
        {
            "sample_id",
            "project_entry_id",
            "slide_id",
            "section_id",
            "canonical_image_sha256",
            "tissue_generation_id",
            "segmentation_population_id",
            "phenotype_generation_id",
            "cluster_label_import_result_ref_sha256",
            "raw_export_ref_sha256",
            "raw_export_sha256",
            "cell_ids_sha256",
            "cell_to_domain_membership_sha256",
            "phenotype_view_artifact_sha256",
            "phenotype_view_lineage_sha256",
            "phenotype_view_inventory_sha256",
            "saved_state_after_sha256",
            "hierarchy_evidence_sha256",
            "attestation_sha256",
        }
    )
)


# ==============================================================================
#                              Application records
# ==============================================================================


def phenotype_hierarchy_attestation_identity_contract() -> ArtifactIdentityContract:
    return ArtifactIdentityContract(
        contract_id="mif-batch-phenotype-view-attestation-identity/v1",
        artifact_kind="phenotype_view_attestation",
        schema_name="mif-batch-phenotype-view-attestation",
        schema_version=1,
        required_fields=_ATTESTATION_IDENTITY_FIELDS,
        optional_fields=(),
        required_runtime_names=("mif_pipeline",),
    )


def semantic_raw_inventory_identity_contract() -> ArtifactIdentityContract:
    """Public owner identity for the strict existing raw QuPath export."""

    return ArtifactIdentityContract(
        contract_id="mif-batch-semantic-raw-inventory-identity/v1",
        artifact_kind="semantic_raw_inventory",
        schema_name="qupath-raw-objects",
        schema_version=1,
        required_fields=_SEMANTIC_RAW_INVENTORY_IDENTITY_FIELDS,
        optional_fields=(),
        required_runtime_names=("mif_pipeline", "qupath"),
    )


def _freeze_mapping(value: Mapping[str, object], field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise BatchPhenotypingError(f"{field} must be a string-keyed mapping")
    try:
        import json

        plain = json.loads(canonical_bytes(value).decode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise BatchPhenotypingError(f"{field} is not canonical JSON") from exc

    def freeze(item: object) -> object:
        if isinstance(item, dict):
            return MappingProxyType({key: freeze(child) for key, child in sorted(item.items())})
        if isinstance(item, list):
            return tuple(freeze(child) for child in item)
        return item

    result = freeze(plain)
    assert isinstance(result, Mapping)
    return result


def _canonical_cell_domains(
    value: Mapping[str, str], *, domain_ids: frozenset[str]
) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise BatchPhenotypingError("cell_domains must be a nonempty mapping")
    result: dict[str, str] = {}
    for cell_id, domain_id in value.items():
        cell = text(cell_id, "cell_domains cell_id")
        domain = text(domain_id, "cell_domains analysis_domain_id")
        if domain not in domain_ids:
            raise BatchPhenotypingError("cell_domains names a foreign analysis domain")
        if cell in result:
            raise BatchPhenotypingError("cell_domains contains a duplicate cell ID")
        result[cell] = domain
    return MappingProxyType(dict(sorted(result.items())))


@dataclass(frozen=True, slots=True)
class ClusterLabelSectionPopulation:
    """Exact local population reopened from current section authorities.

    Local cell IDs are meaningful only inside ``project_entry_id`` and
    ``section_id``.  Cross-image code must join them to composite clustering
    identities before constructing this record.
    """

    sample_id: str
    project_entry_id: str
    slide_id: str
    canonical_image_id: str
    canonical_image_sha256: str
    section_id: str
    tissue_generation_id: str
    tissue_path_object_id: str
    tissue_roi_id: str
    segmentation_generation_id: str
    segmentation_population_id: str
    measurement_generation_id: str
    domain_kinds: Mapping[str, AnalysisDomainKind]
    cell_domains: Mapping[str, str]
    selected_cell_ids: tuple[str, ...]
    cell_ids_sha256: str
    cell_to_domain_membership_sha256: str
    segmentation_result_ref_sha256: str
    measurements_result_ref_sha256: str
    accepted_domain_ref_sha256s: tuple[str, ...]
    phenotyping_mode_ref_sha256s: tuple[str, ...]
    saved_state_after_sha256: str

    def __post_init__(self) -> None:
        """Validate the ClusterLabelSectionPopulation fields when the record is constructed.

        The checks cover ``cell_domains``, ``cell_ids_sha256``,
        ``cell_to_domain_membership_sha256``, ``domain_kinds``. Invalid field types, values
        or combinations raise the declared validation error before this instance can be used
        by its caller."""

        for name in (
            "sample_id",
            "project_entry_id",
            "slide_id",
            "canonical_image_id",
            "section_id",
            "tissue_generation_id",
            "tissue_path_object_id",
            "tissue_roi_id",
            "segmentation_generation_id",
            "segmentation_population_id",
            "measurement_generation_id",
        ):
            object.__setattr__(self, name, text(getattr(self, name), name))
        for name in (
            "canonical_image_sha256",
            "cell_ids_sha256",
            "cell_to_domain_membership_sha256",
            "segmentation_result_ref_sha256",
            "measurements_result_ref_sha256",
            "saved_state_after_sha256",
        ):
            object.__setattr__(self, name, sha256(getattr(self, name), name))
        if not isinstance(self.domain_kinds, Mapping) or not self.domain_kinds:
            raise BatchPhenotypingError("domain_kinds must be a nonempty mapping")
        kinds: dict[str, AnalysisDomainKind] = {}
        for domain_id, raw_kind in self.domain_kinds.items():
            domain = text(domain_id, "domain_kinds analysis_domain_id")
            try:
                kind = AnalysisDomainKind(raw_kind)
            except (TypeError, ValueError) as exc:
                raise BatchPhenotypingError("domain_kinds contains an invalid kind") from exc
            kinds[domain] = kind
        if len(kinds) != len(self.domain_kinds):
            raise BatchPhenotypingError("domain_kinds contains duplicate IDs")
        frozen_kinds = MappingProxyType(dict(sorted(kinds.items())))
        cells = _canonical_cell_domains(self.cell_domains, domain_ids=frozenset(frozen_kinds))
        if set(cells.values()) != set(frozen_kinds):
            raise BatchPhenotypingError(
                "cell population does not cover the accepted domain inventory"
            )
        if self.cell_ids_sha256 != digest(list(cells)):
            raise BatchPhenotypingError("section cell inventory digest differs")
        if self.cell_to_domain_membership_sha256 != digest(dict(cells)):
            raise BatchPhenotypingError("section cell/domain mapping digest differs")
        for name in ("accepted_domain_ref_sha256s", "phenotyping_mode_ref_sha256s"):
            values = exact_tuple(getattr(self, name), name)
            if not values or values != tuple(sorted(set(values))):
                raise BatchPhenotypingError(f"{name} must be sorted, unique and nonempty")
            for value in values:
                sha256(value, name)
            object.__setattr__(self, name, values)
        object.__setattr__(self, "domain_kinds", frozen_kinds)
        selected = exact_tuple(self.selected_cell_ids, "selected_cell_ids")
        if selected != tuple(sorted(set(selected))) or not set(selected) <= set(cells):
            raise BatchPhenotypingError("Selected phenotyping cell IDs must be unique current cells")
        object.__setattr__(self, "cell_domains", cells)
        object.__setattr__(self, "selected_cell_ids", selected)

    @classmethod
    def create(cls, **values: object) -> "ClusterLabelSectionPopulation":
        cell_domains = values.get("cell_domains")
        if not isinstance(cell_domains, Mapping):
            raise BatchPhenotypingError("cell_domains must be a mapping")
        values = dict(values)
        values["cell_ids_sha256"] = digest(sorted(cell_domains))
        values["cell_to_domain_membership_sha256"] = digest(dict(sorted(cell_domains.items())))
        return cls(**values)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ClusterLabelSectionApplication:
    sample_id: str
    project_entry_id: str
    slide_id: str
    canonical_image_id: str
    canonical_image_sha256: str
    section_id: str
    tissue_generation_id: str
    tissue_path_object_id: str
    tissue_roi_id: str
    segmentation_generation_id: str
    segmentation_population_id: str
    measurement_generation_id: str
    clustering_generation_sha256: str
    accepted_review_sha256: str
    composite_projection_sha256: str
    cell_ids_sha256: str
    cell_to_domain_membership_sha256: str
    phenotype_generation_id: str
    import_rows: tuple[Mapping[str, object], ...]
    artifact: Mapping[str, object]
    source_ref_sha256s: tuple[str, ...]
    expected_saved_state_before_sha256: str

    def __post_init__(self) -> None:
        """Validate the ClusterLabelSectionApplication fields when the record is constructed.

        The checks cover ``import_rows``, ``source_ref_sha256s``, ``artifact``,
        ``cell_ids_sha256``, ``section_id``, ``phenotype_generation_id``. Invalid field
        types, values or combinations raise the declared validation error before this
        instance can be used by its caller."""

        for name in (
            "sample_id",
            "project_entry_id",
            "slide_id",
            "canonical_image_id",
            "section_id",
            "tissue_generation_id",
            "tissue_path_object_id",
            "tissue_roi_id",
            "segmentation_generation_id",
            "segmentation_population_id",
            "measurement_generation_id",
            "phenotype_generation_id",
        ):
            object.__setattr__(self, name, text(getattr(self, name), name))
        for name in (
            "canonical_image_sha256",
            "clustering_generation_sha256",
            "accepted_review_sha256",
            "composite_projection_sha256",
            "cell_ids_sha256",
            "cell_to_domain_membership_sha256",
            "expected_saved_state_before_sha256",
        ):
            object.__setattr__(self, name, sha256(getattr(self, name), name))
        rows = exact_tuple(self.import_rows, "import_rows")
        if not rows:
            raise BatchPhenotypingError("cluster-label application cannot be empty")
        frozen_rows = tuple(
            _freeze_mapping(item, "import row") for item in rows  # type: ignore[arg-type]
        )
        ids = tuple(str(item["cell_id"]) for item in frozen_rows)
        if ids != tuple(sorted(set(ids))) or digest(list(ids)) != self.cell_ids_sha256:
            raise BatchPhenotypingError("import rows do not match exact cell inventory")
        refs = exact_tuple(self.source_ref_sha256s, "source_ref_sha256s")
        if refs != tuple(sorted(set(refs))) or not refs:
            raise BatchPhenotypingError("application source refs must be sorted unique")
        for value in refs:
            sha256(value, "source_ref_sha256s")
        artifact = _freeze_mapping(self.artifact, "phenotype artifact")
        if (
            artifact.get("mode") != "unsupervised_clustering"
            or artifact.get("section_id") != self.section_id
            or artifact.get("phenotype_generation_id") != self.phenotype_generation_id
            or tuple(artifact.get("cell_ids", ())) != ids
            or artifact.get("cell_ids_sha256") != self.cell_ids_sha256
        ):
            raise BatchPhenotypingError("phenotype artifact differs from application")
        object.__setattr__(self, "import_rows", frozen_rows)
        object.__setattr__(self, "artifact", artifact)
        object.__setattr__(self, "source_ref_sha256s", refs)


@dataclass(frozen=True, slots=True)
class SemanticRawSnapshot:
    """Our compact current raw-export scope, count and stable identity summaries."""

    sample_id: str
    project_entry_id: str
    slide_id: str
    section_id: str
    canonical_image_sha256: str
    tissue_generation_id: str
    segmentation_population_id: str
    raw_export_ref_sha256: str
    raw_export_sha256: str
    saved_state_after_sha256: str
    cell_count: int
    analysis_domain_ids: tuple[str, ...]
    cell_ids_sha256: str
    cell_to_domain_membership_sha256: str
    primary_domain_status: str

    def __post_init__(self) -> None:
        for name in (
            "sample_id",
            "project_entry_id",
            "slide_id",
            "section_id",
            "tissue_generation_id",
            "segmentation_population_id",
        ):
            object.__setattr__(self, name, text(getattr(self, name), name))
        for name in (
            "canonical_image_sha256",
            "raw_export_ref_sha256",
            "raw_export_sha256",
            "saved_state_after_sha256",
            "cell_ids_sha256",
            "cell_to_domain_membership_sha256",
        ):
            object.__setattr__(self, name, sha256(getattr(self, name), name))
        if type(self.cell_count) is not int or self.cell_count < 0:
            raise BatchPhenotypingError("semantic raw snapshot requires its cell count")
        if type(self.primary_domain_status) is not str or self.primary_domain_status not in {
            "active", "excluded_below_minimum", "excluded_by_primary_count_review"
        } or (self.primary_domain_status != "active" and self.cell_count != 0):
            raise BatchPhenotypingError("semantic raw primary disposition differs from its cell population")
        domains = exact_tuple(self.analysis_domain_ids, "analysis_domain_ids")
        if domains != tuple(sorted(set(domains))) or any(not isinstance(item, str) or not item for item in domains):
            raise BatchPhenotypingError("semantic raw snapshot requires its exact domain scope")


@dataclass(frozen=True, slots=True)
class PhenotypeHierarchyAttestation:
    sample_id: str
    project_entry_id: str
    slide_id: str
    section_id: str
    canonical_image_sha256: str
    tissue_generation_id: str
    segmentation_population_id: str
    phenotype_generation_id: str
    cluster_label_import_result_ref_sha256: str
    raw_export_ref_sha256: str
    raw_export_sha256: str
    cell_ids_sha256: str
    cell_to_domain_membership_sha256: str
    phenotype_view_artifact_sha256: str
    phenotype_view_lineage_sha256: str
    phenotype_view_inventory_sha256: str
    saved_state_after_sha256: str
    hierarchy_evidence_sha256: str
    attestation_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "sample_id",
            "project_entry_id",
            "slide_id",
            "section_id",
            "tissue_generation_id",
            "segmentation_population_id",
            "phenotype_generation_id",
        ):
            object.__setattr__(self, name, text(getattr(self, name), name))
        for name in self.__dataclass_fields__:
            if name.endswith("_sha256"):
                object.__setattr__(self, name, sha256(getattr(self, name), name))
        if self.attestation_sha256 != digest(self.material_mapping()):
            raise BatchPhenotypingError("phenotype hierarchy attestation digest differs")

    def material_mapping(self) -> dict[str, object]:
        return {
            "schema_version": PHENOTYPE_HIERARCHY_ATTESTATION_SCHEMA,
            **{
                name: getattr(self, name)
                for name in self.__dataclass_fields__
                if name != "attestation_sha256"
            },
        }

    def to_mapping(self) -> dict[str, object]:
        return {**self.material_mapping(), "attestation_sha256": self.attestation_sha256}

    @classmethod
    def from_mapping(cls, value: object) -> "PhenotypeHierarchyAttestation":
        if not isinstance(value, Mapping) or set(value) != {
            "schema_version",
            *cls.__dataclass_fields__,
        }:
            raise BatchPhenotypingError("phenotype hierarchy attestation mapping is not closed")
        if value["schema_version"] != PHENOTYPE_HIERARCHY_ATTESTATION_SCHEMA:
            raise BatchPhenotypingError("phenotype hierarchy attestation schema is unsupported")
        return cls(
            **{name: value[name] for name in cls.__dataclass_fields__}
        )  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True, init=False)
class ValidatedClusterLabelImportProjection:
    """Decoder-only compact view of one committed native import result."""

    artifact_ref: BatchArtifactRef
    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str
    identity: Mapping[str, object]

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("cluster-label import projection is decoder-only")


@dataclass(frozen=True, slots=True, init=False)
class ValidatedSemanticRawInventoryProjection:
    """Our decoded current raw-export identity and compact live scope binding."""

    artifact_ref: BatchArtifactRef
    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str
    phenotype_source_artifact_ref_sha256: str
    configured_secondary_ids: tuple[str, ...]
    candidate_secondary_roi_ids: tuple[str, ...]
    snapshot: SemanticRawSnapshot
    semantic_live_binding: Mapping[str, object]

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("semantic raw inventory projection is decoder-only")


@dataclass(frozen=True, slots=True, init=False)
class ValidatedPhenotypeHierarchyAttestationProjection:
    """Decoder-only view of one persisted committed hierarchy attestation."""

    artifact_ref: BatchArtifactRef
    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str
    attestation: PhenotypeHierarchyAttestation

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("phenotype hierarchy attestation projection is decoder-only")


__all__ = [
    "ClusterLabelSectionApplication",
    "ClusterLabelSectionPopulation",
    "PHENOTYPE_HIERARCHY_ATTESTATION_SCHEMA",
    "PhenotypeHierarchyAttestation",
    "SemanticRawSnapshot",
    "ValidatedClusterLabelImportProjection",
    "ValidatedPhenotypeHierarchyAttestationProjection",
    "ValidatedSemanticRawInventoryProjection",
    "phenotype_hierarchy_attestation_identity_contract",
    "semantic_raw_inventory_identity_contract",
]
