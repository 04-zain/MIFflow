"""

Connects adjacent image patches for the KRONOS spatial-feature calculation.

We represent each image patch as one graph node. A connection joins that
patch to an existing patch immediately left, right, above or below, provided
both belong to the same image, accepted region and training/evaluation group.
These connections identify which neighbouring image features contribute to
the current patch's learned spatial features.

We store at most four neighbour positions per patch, using four int32 values.
Calculating the current one-layer model requires the selected patch and
these immediate neighbours. Selecting a small set of predictions therefore
requires only the encoder vectors contributing to those calculations.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable

import numpy as np

# ==============================================================================
#                                  Patch graph
# ==============================================================================


class GraphInputError(ValueError):
    """

    Reports an invalid patch description, grid position or requested prediction input.
    """

class GraphBudgetError(GraphInputError):
    """

    Reports that the required graph or numerical arrays exceed the selected memory limit.
    """

@dataclass(frozen=True)
class PatchScope:
    """

    Records the image, selected tissue and evaluation role that permit patches to share a connection.

    We require every field below to agree before connecting two adjacent grid
    positions. A patch from another image or held-out subject therefore cannot
    supply features to a training patch merely because its grid indices match.

    Attributes:
        patient_id: Subject contributing the image patch.
        image_id: Image from which the patch is read.
        entry_id: Corresponding QuPath project entry.
        split: Training, validation, test or prediction role of the image.
        fold_id: Particular cross-validation fit or prediction group.
        domain_id: Accepted analysis-region identifier.
        domain_revision: Saved version of that analysis region.
        tissue_component_id: Connected tissue component containing the patch.
        eligible_parent_id: Parent compartment or structure in which the
            selected classification is permitted.
    """

    patient_id: str
    image_id: str
    entry_id: str
    split: str
    fold_id: str
    domain_id: str
    domain_revision: str
    tissue_component_id: str
    eligible_parent_id: str

    def __post_init__(self) -> None:
        """

        Checks that each required image, region and evaluation identifier is present.

        Empty identifiers would leave the neighbour comparison unable to distinguish
        the corresponding image or selected tissue. We require explicit nonblank
        text before a patch can be included in a graph.

        Raises:
            GraphInputError: A required identifier is missing or blank.
        """

        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise GraphInputError(f"{name} must be a non-empty string")


@dataclass(frozen=True)
class PatchNode:
    """

    Describes one image patch by its grid position and the image region it belongs to.

    Grid row and column locate the patch relative to the image origin. The
    image-preparation functions determine its physical sampling and pixel
    rectangle before constructing this graph description.

    Attributes:
        node_id: Unique patch identifier used to retrieve its encoder vector.
        grid_row: Zero-based row on the image-patch grid.
        grid_col: Zero-based column on that grid.
        scope: PatchScope identifying its subject, image, accepted region and
            training/evaluation role.
    """

    node_id: str
    grid_row: int
    grid_col: int
    scope: PatchScope

    def __post_init__(self) -> None:
        """

        Checks the patch identifier, image-region record and integer grid coordinates.

        Rows and columns must be nonnegative integers because neighbour lookup uses
        adjacent grid positions. Boolean values are rejected as grid coordinates.

        Raises:
            GraphInputError: The identifier, PatchScope or row/column values are invalid.
        """

        if not isinstance(self.node_id, str) or not self.node_id.strip():
            raise GraphInputError("node_id must be a non-empty string")
        if not isinstance(self.scope, PatchScope):
            raise GraphInputError("scope must be PatchScope")
        for name in ("grid_row", "grid_col"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise GraphInputError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class GraphSelection:
    """

    Identifies the patches to predict and the image features required for their predictions.

    Each prediction uses its own encoder vector and the mean of its immediate
    neighbours. We therefore retain the requested patches separately from the
    larger set of patches whose encoder vectors must be available.

    Attributes:
        targets: Requested patch IDs in the graph's recorded order.
        context: Those target IDs plus their immediate neighbour IDs in the
            same recorded order, with each patch included once.
    """

    targets: tuple[str, ...]
    context: tuple[str, ...]


@dataclass(frozen=True, init=False)
class PatchGraph:
    """

    Stores the immediate image neighbours of every selected patch.

    We arrange rows consistently by image/region identifiers, grid position
    and patch ID. Each row contains four positions corresponding to left,
    right, above and below; -1 records an absent neighbour. Four int32 entries
    require 16 bytes per patch, in addition to the patch descriptions, lookup
    dictionaries and temporary arrays used during construction.

    Attributes:
        nodes: PatchNode descriptions in the recorded row order.
        index_buffer_bytes: Bytes occupied by the stored four-column neighbour array.
        _indices: Neighbour row positions, with -1 for missing positions.
        _by_id: Mapping from patch ID to its row in nodes and _indices.
    """

    nodes: tuple[PatchNode, ...]
    index_buffer_bytes: int
    _indices: np.ndarray = field(repr=False, compare=False)
    _by_id: object = field(repr=False, compare=False)

    def __init__(
        self, inventory: Iterable[PatchNode], max_nodes: int, max_index_bytes: int
    ) -> None:
        """

        Builds the four-neighbour image graph from distinct patch positions within each permitted region.

        i) We collect the selected patch descriptions and check their count against
        max_nodes. Four int32 neighbour positions require 16 bytes per patch; that
        array must fit max_index_bytes before it is allocated.

        ii) We sort the patches by their image/region/role fields, grid position and
        patch ID, then create lookups by ID and by complete region plus row/column.
        Duplicate IDs or duplicate positions within one such region are rejected.

        iii) For a patch at (row,column), we inspect (row,column-1), (row,column+1),
        (row-1,column) and (row+1,column). A neighbour is stored only when that
        adjacent position exists with the same subject, image, fold, selected
        region, tissue component and permitted parent. Missing positions remain -1.

        Args:
            inventory: PatchNode descriptions supplied by the image-preparation step.
            max_nodes: Maximum number of patches permitted in this graph.
            max_index_bytes: Maximum bytes allowed for the four-column int32 neighbour array.

        Raises:
            GraphInputError: A limit is invalid, patches are absent or incorrectly
                described, or IDs/positions repeat.
            GraphBudgetError: Patch count or neighbour-array storage exceeds its
                limit or the supported int32 index range.
        """

        for name, value in (("max_nodes", max_nodes), ("max_index_bytes", max_index_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise GraphInputError(f"{name} must be a positive integer")
        try:
            iterator = iter(inventory)
        except TypeError as exc:
            raise GraphInputError("inventory must be an iterable of PatchNode") from exc
        nodes = []
        for node in iterator:
            if not isinstance(node, PatchNode):
                raise GraphInputError("inventory must contain PatchNode records")
            nodes.append(node)
            if len(nodes) > max_nodes:
                raise GraphBudgetError("inventory exceeds max_nodes")
        if not nodes:
            raise GraphInputError("inventory must not be empty")
        index_bytes = 16 * len(nodes)
        if index_bytes > max_index_bytes:
            raise GraphBudgetError("stored index buffer exceeds max_index_bytes")
        nodes.sort(
            key=lambda n: (
                tuple(getattr(n.scope, name) for name in n.scope.__dataclass_fields__),
                n.grid_row,
                n.grid_col,
                n.node_id,
            )
        )
        by_id: dict[str, int] = {}
        by_position: dict[tuple[PatchScope, int, int], int] = {}
        for index, node in enumerate(nodes):
            if node.node_id in by_id:
                raise GraphInputError(f"duplicate node_id: {node.node_id}")
            position = (node.scope, node.grid_row, node.grid_col)
            if position in by_position:
                raise GraphInputError("duplicate scope/grid position")
            by_id[node.node_id] = index
            by_position[position] = index
        if len(nodes) > np.iinfo(np.int32).max:
            raise GraphBudgetError("node count exceeds int32 index range")
        # Store one slot for each permitted direction. Missing positions stay -1.
        indices = np.full((len(nodes), 4), -1, dtype=np.int32)
        for index, node in enumerate(nodes):
            for slot, (dr, dc) in enumerate(((0, -1), (0, 1), (-1, 0), (1, 0))):
                # Match both grid position and the complete image/region
                # identifiers, including the selected evaluation role.
                neighbour = by_position.get((node.scope, node.grid_row + dr, node.grid_col + dc))
                if neighbour is not None:
                    indices[index, slot] = neighbour
        immutable_indices = np.frombuffer(indices.tobytes(), dtype=np.int32).reshape(-1, 4)
        object.__setattr__(self, "nodes", tuple(nodes))
        object.__setattr__(self, "index_buffer_bytes", index_bytes)
        object.__setattr__(self, "_indices", immutable_indices)
        object.__setattr__(self, "_by_id", MappingProxyType(by_id))

    def neighbors(self, node_id: str) -> tuple[str, ...]:
        """

        Retrieves the existing immediate neighbours of one patch from the stored graph.

        We read its four neighbour slots and omit positions recorded as missing.
        This reuses the image and region restrictions checked during construction.

        Args:
            node_id: Patch whose adjacent encoder vectors will contribute to prediction.

        Returns:
            Neighbour IDs in left, right, above and below order where present.

        Raises:
            GraphInputError: The patch ID is invalid or absent from this graph.
        """

        if not isinstance(node_id, str):
            raise GraphInputError("node_id must be a string")
        index = self._by_id.get(node_id)
        if index is None:
            raise GraphInputError(f"unknown node_id: {node_id}")
        return tuple(self.nodes[int(i)].node_id for i in self._indices[index] if i >= 0)

    def select(self, target_ids: Iterable[str]) -> GraphSelection:
        """

        Collects requested patches and the immediate neighbours needed to calculate their spatial features.

        We check that each requested ID exists and occurs once, then add its
        recorded neighbours to the feature-read set. The current model combines
        original encoder vectors through one layer, so only these immediate
        neighbours are required. Both returned sequences follow the graph's row order.

        Args:
            target_ids: Distinct patch IDs for the requested predictions.

        Returns:
            GraphSelection containing prediction targets and the larger set of
            patch IDs whose encoder vectors must be supplied.

        Raises:
            GraphInputError: The selection is empty or contains invalid, repeated
                or unknown patch IDs.
        """

        if isinstance(target_ids, (str, bytes)):
            raise GraphInputError("target_ids must be an iterable of node IDs")
        try:
            iterator = iter(target_ids)
        except TypeError as exc:
            raise GraphInputError("target_ids must be an iterable of node IDs") from exc
        requested: set[str] = set()
        for node_id in iterator:
            if not isinstance(node_id, str) or not node_id.strip():
                raise GraphInputError("target node_id must be a non-empty string")
            if node_id in requested:
                raise GraphInputError(f"duplicate target node_id: {node_id}")
            if node_id not in self._by_id:
                raise GraphInputError(f"unknown target node_id: {node_id}")
            requested.add(node_id)
        if not requested:
            raise GraphInputError("target_ids must not be empty")
        context = set(requested)
        for node_id in requested:
            context.update(self.neighbors(node_id))
        return GraphSelection(
            tuple(node.node_id for node in self.nodes if node.node_id in requested),
            tuple(node.node_id for node in self.nodes if node.node_id in context),
        )
