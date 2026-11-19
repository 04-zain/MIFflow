"""

Selects the cells and colours displayed from an existing two-dimensional coordinate view.

We begin with the coordinates already fitted by UMAP and their aligned
cell IDs, measurements and category labels. For continuous colouring, the
minimum and maximum use every cell in this supplied coordinate view. For
categorical colouring, we collect its complete category list and assign a
repeatable colour to each label.

We can then select fewer points for drawing while retaining those same
coordinates and colour definitions. This is a second display selection;
the preceding UMAP fit may already have used a sample of the full analysis.
Its settings and this plotted subset are recorded separately so the
displayed population remains identifiable.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from math import isfinite
from types import MappingProxyType
from typing import Mapping

from .embeddings import EmbeddingArtifact, EmbeddingError

_CATEGORICAL_ATTRIBUTES = frozenset(
    {"cluster_id", "section_id", "primary_compartment", "display_label"}
)
_DECIMATION_ALGORITHM = "blake2b-128-stable-id-rank"
_SCHEMA_VERSION = "cell-embedding-plot/v1"


# ==============================================================================
#                                Embedding plots
# ==============================================================================


@dataclass(frozen=True, slots=True)
class EmbeddingPlotArtifact:
    """

    Records the exact cells, coordinates and colours used for one scatter display.

    The plot retains the ID and checksum of its fitted coordinate view, then
    adds the selected cells and colour settings. Changing the displayed subset
    or colouring produces a new plot identity while retaining its association
    with those fitted coordinates.

    Attributes:
        schema_version: Current cell-embedding-plot/v1 format.
        parent_artifact_id: ID of the fitted coordinate view supplying the points.
        parent_content_hash: Checksum of that coordinate view.
        display_cell_ids: Unique cell IDs selected for drawing, in plotted order.
        coordinates: Finite two-dimensional positions aligned with those cells.
        colors: Original measurement values or category labels for the same points.
        color_metadata: Continuous feature/units/colour limits or the complete
            category-to-colour mapping.
        rendering_parameters: Maximum point count, selected count, seed,
            selection method and displayed cell IDs.
        content_hash: Checksum of the complete plot description.
        plot_id: Saved plot ID formed from the checksum.
    """

    schema_version: str
    parent_artifact_id: str
    parent_content_hash: str
    display_cell_ids: tuple[str, ...]
    coordinates: tuple[tuple[float, float], ...]
    colors: tuple[float | str, ...]
    color_metadata: Mapping[str, object]
    rendering_parameters: Mapping[str, object]
    content_hash: str = field(init=False)
    plot_id: str = field(init=False)

    def __post_init__(self) -> None:
        """

        Checks point alignment and records the identity of the complete plotted values.

        We require the fitted view's identity, unique displayed cell IDs and equal
        lengths for cells, coordinates and colour values. Every coordinate must
        contain two finite numbers. Colour and rendering dictionaries are copied
        recursively before the complete description is encoded and hashed.

        Raises:
            EmbeddingError: Format, fitted-view identity, coordinate shape or cell
                alignment is invalid.
            TypeError or ValueError: Other plot fields cannot be encoded as finite JSON.
        """

        if self.schema_version != _SCHEMA_VERSION:
            raise EmbeddingError(f"plot schema_version must be {_SCHEMA_VERSION!r}")
        if not self.parent_artifact_id or not self.parent_content_hash:
            raise EmbeddingError("plot artifact must link to its parent embedding content")
        if len(set(self.display_cell_ids)) != len(self.display_cell_ids):
            raise EmbeddingError("display cell IDs must be unique")
        if not (len(self.display_cell_ids) == len(self.coordinates) == len(self.colors)):
            raise EmbeddingError("display scatter data must align with display cell IDs")
        if any(
            len(point) != 2 or not all(isfinite(float(value)) for value in point)
            for point in self.coordinates
        ):
            raise EmbeddingError("display coordinates must be finite two-dimensional points")
        object.__setattr__(self, "color_metadata", _freeze_mapping(self.color_metadata))
        object.__setattr__(self, "rendering_parameters", _freeze_mapping(self.rendering_parameters))
        content_hash = _content_hash(self._content_mapping())
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(self, "plot_id", f"cell-embedding-plot:{content_hash}")

    def to_export_mapping(self) -> dict[str, object]:
        """

        Converts the plot description and its identity to ordinary export containers.

        Returns:
            Dictionary containing selected cells, coordinates, colour values,
            rendering settings, content checksum and plot ID.
        """

        return _export(
            {
                **self._content_mapping(),
                "content_hash": self.content_hash,
                "plot_id": self.plot_id,
            }
        )

    def _content_mapping(self) -> Mapping[str, object]:
        """

        Collects the fitted-view reference and plotted values used to identify this display.

        Returns:
            Mapping of the current format, fitted-view ID/checksum, selected cells,
            coordinates, colour values and rendering settings.
        """

        return {
            "schema_version": self.schema_version,
            "parent_artifact_id": self.parent_artifact_id,
            "parent_content_hash": self.parent_content_hash,
            "display_cell_ids": self.display_cell_ids,
            "coordinates": self.coordinates,
            "colors": self.colors,
            "color_metadata": self.color_metadata,
            "rendering_parameters": self.rendering_parameters,
        }


EmbeddingFigure = EmbeddingPlotArtifact


def shade_embedding(
    artifact: EmbeddingArtifact,
    attribute: str,
    *,
    max_display_points: int = 50_000,
    display_seed: int = 0,
) -> EmbeddingPlotArtifact:
    """

    Creates a display of fitted coordinates coloured by a selected measurement or category.

    For a feature ID, we retain its original values and measurement units, use
    the supplied coordinate view's minimum and maximum as colour limits, and
    select the continuous viridis colour map. The limits are calculated before
    choosing the drawing subset, so changing only the number of visible points
    leaves the colour interpretation unchanged.

    For cluster, section, primary compartment or display label, we collect all
    categories present in that coordinate view. Each label receives the repeatable
    colour calculated by _category_color(). We then choose at most the selected
    number of displayed cells and apply the same indices to coordinates and
    colour values.

    Args:
        artifact: Available fitted coordinate view with cell IDs and aligned
            feature/category values.
        attribute: Selected feature ID or one of cluster_id, section_id,
            primary_compartment and display_label.
        max_display_points: Maximum number of points drawn; initially 50,000.
        display_seed: Integer used for reproducible point selection.

    Returns:
        Plot record containing the selected coordinates, original colour values,
        full supplied-view colour limits and exact displayed cell IDs.

    Raises:
        EmbeddingError: The coordinate view is unavailable, the selected
            attribute is absent or point-count/seed settings are invalid.
    """

    if not isinstance(artifact, EmbeddingArtifact):
        raise EmbeddingError("artifact must be an EmbeddingArtifact")
    if not artifact.is_available:
        raise EmbeddingError("cannot shade an unavailable embedding artifact")
    if not isinstance(attribute, str) or not attribute.strip():
        raise EmbeddingError("shading attribute must be a non-empty string")
    if (
        isinstance(max_display_points, bool)
        or not isinstance(max_display_points, int)
        or max_display_points < 1
    ):
        raise EmbeddingError("max_display_points must be a positive integer")
    if isinstance(display_seed, bool) or not isinstance(display_seed, int):
        raise EmbeddingError("display_seed must be an integer")
    indices = _display_indices(artifact.cell_ids, max_display_points, display_seed)
    if attribute in artifact.feature_values:
        values = artifact.feature_values[attribute]
        spec = next(
            spec
            for spec in artifact.feature_specs
            if (spec.feature_id or spec.measurement) == attribute
        )
        # Use the complete embedding population for colour limits, even
        # when only a smaller reproducible subset of points will be drawn.
        metadata: Mapping[str, object] = {
            "kind": "continuous",
            "feature_id": attribute,
            "measurement": spec.measurement,
            "role": spec.role.value,
            "units": spec.units,
            "transform": "identity",
            "clipping": None,
            "colormap": "viridis",
            "vmin": min(values),
            "vmax": max(values),
        }
    elif attribute in _CATEGORICAL_ATTRIBUTES:
        values = artifact.categorical_values[attribute]
        categories = tuple(sorted(set(values)))
        metadata = {
            "kind": "categorical",
            "attribute": attribute,
            "categories": categories,
            "transform": "identity",
            "clipping": None,
            "category_to_color": {category: _category_color(category) for category in categories},
        }
    else:
        raise EmbeddingError(f"unknown embedding shading attribute {attribute!r}")
    return EmbeddingPlotArtifact(
        schema_version=_SCHEMA_VERSION,
        parent_artifact_id=artifact.artifact_id,
        parent_content_hash=artifact.content_hash,
        display_cell_ids=tuple(artifact.cell_ids[index] for index in indices),
        coordinates=tuple(artifact.coordinates[index] for index in indices),
        colors=tuple(values[index] for index in indices),
        color_metadata=metadata,
        rendering_parameters={
            "max_display_points": max_display_points,
            "displayed_point_count": len(indices),
            "decimation_algorithm": _DECIMATION_ALGORITHM,
            "decimation_seed": display_seed,
            "displayed_cell_ids": tuple(artifact.cell_ids[index] for index in indices),
        },
    )


def _display_indices(
    cell_ids: tuple[str, ...], max_display_points: int, seed: int
) -> tuple[int, ...]:
    """

    Selects a repeatable subset of cells when the fitted coordinate view exceeds the drawing limit.

    We calculate a 128-bit BLAKE2b value from the seed and each cell ID, rank
    cells by those values and retain the required lowest ranks. Sorting the
    selected row indices then restores their order in the original coordinate
    view. All indices are retained when the view already fits within the limit.

    This display selection ranks the entire fitted view together. The earlier
    UMAP sampling step separately handles cluster/section/compartment representation;
    a smaller drawing subset does not independently guarantee every group appears.

    Args:
        cell_ids: Unique cell IDs in the fitted coordinate order.
        max_display_points: Positive maximum number of points to draw.
        seed: Recorded integer controlling the ranking.

    Returns:
        Tuple of selected indices in the supplied coordinate order.
    """

    if len(cell_ids) <= max_display_points:
        return tuple(range(len(cell_ids)))
    ranked = sorted(
        range(len(cell_ids)),
        key=lambda index: hashlib.blake2b(
            f"{seed}\0{cell_ids[index]}".encode("utf-8"), digest_size=16
        ).digest(),
    )
    # Preserve the original coordinate/value order after choosing IDs.
    return tuple(sorted(ranked[:max_display_points]))


def _category_color(category: str) -> str:
    """

    Calculates a repeatable RGB colour from a category label.

    The label's SHA-256 supplies a stable hue and saturation. Bright colours
    are darkened for the light plot background, preserving that hue and
    saturation. The same label keeps its colour across plots even when the
    surrounding categories change.

    Args:
        category: Exact category text, including its capitalization.

    Returns:
        Colour in #RRGGBB form. This name-based calculation can give similar
        colours to different labels; it is not a perceptual colour-spacing rule.
    """

    return _readable_category_color(
        "#"
        + hashlib.sha256(f"cell-embedding-category/v1\0{category}".encode("utf-8")).hexdigest()[:6]
    )


def _readable_category_color(color: str) -> str:
    """Keep categorical points visible on light plots, including saved palettes."""
    from colorsys import hls_to_rgb, rgb_to_hls
    from matplotlib.colors import to_rgb

    def luminance(rgb):
        linear = (value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
                  for value in rgb)
        return sum(weight * value for weight, value in zip((.2126, .7152, .0722), linear))

    rgb = to_rgb(color)
    # At most .24 luminance gives at least 3:1 against the desktop page.
    # Leave compliant colours intact, so replaying a saved palette is idempotent.
    if luminance(rgb) <= .24:
        return color
    hue, lightness, saturation = rgb_to_hls(*rgb)
    low, high = 0., lightness
    for _ in range(20):
        middle = (low + high) / 2
        if luminance(hls_to_rgb(hue, middle, saturation)) <= .24:
            low = middle
        else:
            high = middle
    rgb = hls_to_rgb(hue, low, saturation)
    return '#' + ''.join(f'{int(value * 255):02x}' for value in rgb)


def _freeze_mapping(values: Mapping[str, object]) -> Mapping[str, object]:
    """

    Retains a read-only copy of the plot's colour or rendering settings.

    Args:
        values: Mapping whose nested lists and dictionaries are copied recursively.

    Returns:
        Read-only mapping with tuple-valued sequences.

    Raises:
        EmbeddingError: values does not implement Mapping.
    """

    if not isinstance(values, Mapping):
        raise EmbeddingError("plot metadata must be mappings")
    return MappingProxyType({key: _freeze(value) for key, value in values.items()})


def _freeze(value: object) -> object:
    """

    Recursively copies plot-setting collections into read-only mappings and tuples.

    Args:
        value: Mapping, sequence or scalar within the plot settings.

    Returns:
        Read-only collection representation, retaining scalar values.
    """

    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _content_hash(value: Mapping[str, object]) -> str:
    """

    Calculates the checksum of the fitted-view reference and complete plotted data.

    Args:
        value: Plot-content mapping before adding its calculated checksum.

    Returns:
        SHA-256 of sorted-key, finite JSON with displayed point order retained.

    Raises:
        TypeError or ValueError: A value cannot be serialized or contains NaN/infinity.
    """

    payload = json.dumps(
        value, default=_json_default, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_default(value: object) -> object:
    """

    Converts supported read-only collections for JSON serialization.

    Args:
        value: Mapping or tuple passed to the encoder's conversion callback.

    Returns:
        Ordinary dictionary or list.

    Raises:
        TypeError: The encoder encounters another unsupported object type.
    """

    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"unsupported embedding plot value {type(value).__name__}")


def _export(value: object) -> object:
    """

    Recursively copies a plot record into JSON-compatible dictionaries and lists.

    Args:
        value: Stored plot data with mappings, tuples and scalar values.

    Returns:
        Exportable containers preserving cell and coordinate order.
    """

    if isinstance(value, Mapping):
        return {str(key): _export(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_export(item) for item in value]
    return value


__all__ = ["EmbeddingFigure", "EmbeddingPlotArtifact", "shade_embedding"]
