"""

Checks candidate polygon geometry before semantic measurements are evaluated.

The shared polygon validator handles the supported Polygon/MultiPolygon
representation. This module reports its geometry errors as SemanticValidationError
so the semantic workflow can identify the failed candidate preparation step.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from mif_pipeline.pipeline_core.models import ROIAnalysisError
from mif_pipeline.pipeline_core.polygon_validation import (
    PolygonGeometryError,
    validate_polygonal_geometry as _validate_polygonal_geometry,
)

# ==============================================================================
#                                    Semantic
# ==============================================================================


class SemanticValidationError(ROIAnalysisError):
    """

    Reports a candidate geometry or semantic input that cannot be evaluated safely.
    """

def validate_polygonal_geometry(geometry: object) -> None:
    """

    Checks a candidate's polygon representation through the shared geometry validator.

    Args:
        geometry: Polygonal geometry representation accepted by the pipeline's
            polygon validator, including its required coordinates and structure.

    Raises:
        SemanticValidationError: Geometry validation fails. The underlying explanation
            is retained in the error message and chained exception.
    """

    try:
        _validate_polygonal_geometry(geometry)
    except PolygonGeometryError as exc:
        raise SemanticValidationError(str(exc)) from exc


__all__ = ["SemanticValidationError", "validate_polygonal_geometry"]
