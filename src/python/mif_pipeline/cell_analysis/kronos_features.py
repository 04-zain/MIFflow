"""Shared scientific definition of the native masked KRONOS cell features."""
from types import MappingProxyType

LEGACY_CELL_EXTRACTION = MappingProxyType({
    "schema": "kronos-cell-extraction/v1",
    "center": "nucleus-centroid",
    "mask": "cell-roi",
    "outside": "zero-before-normalization",
    "pooling": "x_norm_clstoken",
    "interpolation": "nearest-level0",
    "edge_policy": "zero-pad",
    "field_of_view": "saved-samples-at-recorded-pixel-size",
})

# New authoring uses area integration only when the requested grid coarsens.
# Accepted v1 sources retain their complete original policy and execution path.
CELL_EXTRACTION = MappingProxyType({
    **LEGACY_CELL_EXTRACTION,
    "schema": "kronos-cell-extraction/v2",
    "mask": "native-pixel-and-target-center-cell-roi",
    "interpolation": "area-overlap-on-coarsening",
})


def validate_cell_extraction(value):
    """Accept only the explicit, versioned masked CLS sampling recipe."""
    if (not isinstance(value, (dict, MappingProxyType))
            or dict(value) not in (dict(LEGACY_CELL_EXTRACTION), dict(CELL_EXTRACTION))):
        raise ValueError("Unsupported KRONOS cell extraction policy")
    return dict(value)

