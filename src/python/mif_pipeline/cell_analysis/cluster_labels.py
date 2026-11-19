"""

Applies the biological names selected during cell-cluster review.

We first calculate cluster membership from the selected cell features.
During review, we associate a cluster ID with a phenotype name where its
marker profile and cell examples support that interpretation. This module
copies those names onto the corresponding cell records and prepares the
rows needed to update their QuPath objects.

Each cell retains its cluster ID alongside the reviewed name. A cluster
omitted from the name mapping remains labelled by its cluster ID. The saved
label-result identifier is calculated from the complete cell membership
and name mapping, so changing a selected name creates a distinct labelling
result for the same cluster solution.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from types import MappingProxyType
from typing import Iterable, Mapping

from mif_pipeline.pipeline_core.models import CellRecord, ROIAnalysisError
from .phenotyping_population import NOT_PHENOTYPED

# ==============================================================================
#                                 Cluster labels
# ==============================================================================


class ClusterLabelError(ROIAnalysisError):
    """

    Reports missing cell-cluster assignments or an invalid cluster-name selection.
    """

@dataclass(frozen=True, slots=True)
class ClusterLabelView:
    """

    Retains the cells and update rows produced by applying the reviewed cluster names.

    Attributes:
        cells: Copied CellRecords in their original input order, with the chosen
            phenotype name and label-result identifier.
        cluster_labels: Read-only mapping from named cluster IDs to phenotype text.
        generation_id: Identifier calculated from the cell memberships and names.
        import_rows: Cell-ID-sorted records connecting these labels to the
            corresponding QuPath cells and analysis regions.
    """

    cells: tuple[CellRecord, ...]
    cluster_labels: Mapping[str, str]
    generation_id: str
    import_rows: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        """

        Copies the result's collections before exposing them for review or import.

        We retain cells as a tuple and copy the label mapping and each import row
        into read-only dictionaries. This keeps later edits to a supplied mapping
        separate from the completed label result.
        """

        object.__setattr__(self, "cells", tuple(self.cells))
        object.__setattr__(self, "cluster_labels", MappingProxyType(dict(self.cluster_labels)))
        object.__setattr__(
            self, "import_rows", tuple(MappingProxyType(dict(row)) for row in self.import_rows)
        )

    def by_id(self, cell_id: str) -> CellRecord:
        """

        Finds one labelled cell by scanning the stored cell IDs.

        Args:
            cell_id: ID of the cell to retrieve.

        Returns:
            CellRecord with the matching ID.

        Raises:
            KeyError: No cell in this result has that ID.
        """

        for cell in self.cells:
            if cell.cell_id == cell_id:
                return cell
        raise KeyError(cell_id)

    @property
    def by_cell_id(self) -> Mapping[str, CellRecord]:
        """

        Builds a cell-ID lookup for repeated retrieval from the labelled result.

        Returns:
            Read-only dictionary mapping every stored cell ID to its CellRecord.
            A caller making many lookups can retain this dictionary and reuse it.
        """

        return MappingProxyType({cell.cell_id: cell for cell in self.cells})


def apply_cluster_labels(
    clustered_cells: Iterable[CellRecord], cluster_labels: Mapping[str, str],
    *, source_identity: str, excluded_cluster_ids: Iterable[str] = (),
) -> ClusterLabelView:
    """

    Applies reviewed phenotype names to the cells assigned to each cluster.

    We first require unique cell IDs and an existing cluster ID for every cell.
    Each supplied name must refer to a cluster represented in that population.
    We then copy the cells with (i) the selected primary phenotype, (ii) its
    label source and (iii) the identifier of this labelling result. Their
    measurements, geometry and original cluster membership are retained.

    A named cluster receives its selected phenotype text and the source
    "expert_cluster_label". An unnamed cluster retains its cluster ID as
    primary_phenotype and uses source="cluster_id". Several clusters can receive
    the same biological name while keeping their distinct numerical memberships.

    Excluded clusters receive Not phenotyped while retaining their cells and
    original cluster assignments. Exclusion changes phenotype summaries, not
    the measured cell population or the fitted graph.

    Finally, we create the rows used to update QuPath, sorting them by cell ID.
    The result identifier is calculated from sorted cell-ID/cluster-ID pairs,
    the complete name mapping and the recorded source identity. Reordering input
    rows leaves the same result; a new application retains its own source association.

    Args:
        clustered_cells: CellRecords carrying the completed cluster assignments.
        cluster_labels: Mapping from any subset of present cluster IDs to
            nonblank phenotype names. Unlisted clusters keep their cluster label.
        source_identity: Identity of the accepted review, original application run
            and measurements used for these labels. Replaying that application
            supplies the same value.
        excluded_cluster_ids: Original cluster IDs whose cells remain unphenotyped.
            These clusters cannot also have a phenotype name.

    Returns:
        ClusterLabelView containing the labelled copies, selected names,
        label-result identifier and ordered QuPath update rows.

    Raises:
        ClusterLabelError: A cell ID repeats, membership is missing, or a name
            is blank or assigned to a cluster absent from the cell population.
    """

    cells = tuple(clustered_cells)
    cluster_ids = _validate_clustered_cells(cells)
    labels = _validated_labels(cluster_labels, cluster_ids)
    excluded_values = tuple(excluded_cluster_ids)
    if (any(type(value) is not str for value in excluded_values)
            or len(set(excluded_values)) != len(excluded_values)
            or not set(excluded_values) <= cluster_ids
            or set(excluded_values).intersection(labels)):
        raise ClusterLabelError("Excluded clusters must be distinct current clusters without phenotype names")
    excluded = frozenset(excluded_values)
    generation_id = _generation_id(cells, labels, excluded, source_identity=source_identity)
    # Keep cluster_id unchanged. Only the reviewed phenotype-name fields
    # and their label-generation reference are replaced on copied cells.
    labelled = tuple(
        replace(
            cell,
            primary_phenotype=(NOT_PHENOTYPED if cell.cluster_id in excluded
                               else labels.get(cell.cluster_id, cell.cluster_id)),
            phenotype_source=(
                "expert_cluster_label" if cell.cluster_id in labels or cell.cluster_id in excluded
                else "cluster_id"
            ),
            cluster_label=labels.get(cell.cluster_id),
            phenotype_components=(() if cell.cluster_id in excluded else cell.phenotype_components),
            phenotype_confidence=(None if cell.cluster_id in excluded else cell.phenotype_confidence),
            phenotyping_generation_id=generation_id,
        )
        for cell in cells
    )
    # Stable cell IDs reconnect the names to the original QuPath cells;
    # sorting also keeps import order independent of input-row order.
    rows = tuple(
        MappingProxyType(
            {
                "cell_id": cell.cell_id,
                "slide_id": cell.slide_id,
                "section_id": cell.section_id,
                "analysis_domain_id": cell.analysis_domain_id,
                "analysis_domain_kind": cell.analysis_domain_kind.value,
                "tissue_roi_id": cell.tissue_roi_id,
                "tissue_generation_id": cell.tissue_generation_id,
                "phenotyping_generation_id": cell.phenotyping_generation_id,
                "primary_phenotype": cell.primary_phenotype,
                "phenotype_components": tuple(cell.phenotype_components),
                "phenotype_source": cell.phenotype_source,
                "phenotype_confidence": cell.phenotype_confidence,
                "cluster_id": cell.cluster_id,
                "cluster_label": cell.cluster_label,
            }
        )
        for cell in sorted(labelled, key=lambda cell: cell.cell_id)
    )
    return ClusterLabelView(
        cells=labelled,
        cluster_labels=labels,
        generation_id=generation_id,
        import_rows=rows,
    )


def _validate_clustered_cells(cells: tuple[CellRecord, ...]) -> frozenset[str]:
    """

    Checks cell-ID uniqueness and collects the cluster IDs represented by the cells.

    We compare the number of IDs with the number of distinct IDs, then require
    nonblank cluster text on every record. The collected set is used to reject
    label choices referring to another cluster solution.

    Args:
        cells: Materialized tuple of cells with completed cluster membership.

    Returns:
        Frozen set of represented cluster IDs; empty when no cells are supplied.

    Raises:
        ClusterLabelError: A cell ID repeats or a cluster assignment is absent or blank.
    """

    ids = tuple(cell.cell_id for cell in cells)
    if len(set(ids)) != len(ids):
        raise ClusterLabelError("cluster label stable cell IDs must be unique")
    cluster_ids: set[str] = set()
    for cell in cells:
        if not isinstance(cell.cluster_id, str) or not cell.cluster_id.strip():
            raise ClusterLabelError(f"cell {cell.cell_id} does not have a non-empty cluster_id")
        cluster_ids.add(cell.cluster_id)
    return frozenset(cluster_ids)


def _validated_labels(labels: Mapping[str, str], cluster_ids: frozenset[str]) -> Mapping[str, str]:
    """

    Checks each selected phenotype name against the current cluster membership.

    Cluster keys and labels must contain text, and every key must occur in the
    cell population. We sort the checked mapping by cluster ID before retaining
    it for deterministic saving and label-result identification.

    Args:
        labels: Proposed cluster-ID-to-phenotype mapping.
        cluster_ids: Cluster IDs present among the checked cells.

    Returns:
        Read-only mapping of the checked cluster names.

    Raises:
        ClusterLabelError: The input is not a mapping, a key or label is blank,
            or a key names an unknown cluster.
    """

    if not isinstance(labels, Mapping):
        raise ClusterLabelError("cluster labels must map exact cluster IDs to display labels")
    result: dict[str, str] = {}
    for cluster_id, label in labels.items():
        if not isinstance(cluster_id, str) or not cluster_id.strip():
            raise ClusterLabelError("cluster label keys must be non-empty strings")
        if cluster_id not in cluster_ids:
            raise ClusterLabelError(f"cluster label references unknown cluster_id: {cluster_id!r}")
        if not isinstance(label, str) or not label.strip():
            raise ClusterLabelError("cluster display labels must be non-empty strings")
        result[cluster_id] = label
    return MappingProxyType(dict(sorted(result.items())))


def _generation_id(cells: tuple[CellRecord, ...], labels: Mapping[str, str],
                   excluded: frozenset[str] = frozenset(), *, source_identity: str) -> str:
    """

    Calculates the identifier for this complete membership and phenotype-name assignment.

    We sort the cells by cell ID and the selected names by cluster ID. Their
    compact JSON representation contains a fixed format name, the cell/cluster
    pairs, the mapping and the recorded source identity. Its SHA-256 is appended
    to the label-result prefix.
    This lets the same reviewed labelling be recognized after row reordering.

    Args:
        cells: Checked cell IDs and cluster memberships.
        labels: Checked cluster-to-name mapping.
        source_identity: Recorded source association for this application.

    Returns:
        Label-result ID beginning with "cluster-label-".
    """

    evidence = {
        "schema_version": "cluster-label-view/v1",
        "source_identity": source_identity,
        "cluster_membership": [
            [cell.cell_id, cell.cluster_id] for cell in sorted(cells, key=lambda cell: cell.cell_id)
        ],
        "cluster_labels": dict(sorted(labels.items())),
        **({"excluded_cluster_ids": sorted(excluded)} if excluded else {}),
    }
    payload = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "cluster-label-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "ClusterLabelError",
    "ClusterLabelView",
    "apply_cluster_labels",
]
