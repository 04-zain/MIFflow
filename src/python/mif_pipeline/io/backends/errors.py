"""Exceptions raised at the Bio-Formats ingestion boundary."""

# ==============================================================================
#                                     Errors
# ==============================================================================


class IngestionError(RuntimeError):
    """Base class for pipeline-owned ingestion failures."""


class SourceNotFoundError(IngestionError, FileNotFoundError):
    pass


class SourceOpenError(IngestionError):
    pass


class UnsupportedFormatError(SourceOpenError):
    pass


class DependencyVersionError(IngestionError):
    pass


class MetadataCanonicalizationError(IngestionError):
    pass


class MetadataConflictError(MetadataCanonicalizationError):
    pass


class MetadataMappingError(MetadataCanonicalizationError):
    pass


class InvalidSeriesError(IngestionError, IndexError):
    pass


class InvalidResolutionError(IngestionError, IndexError):
    pass


class InvalidPlaneError(IngestionError, IndexError):
    pass


class InvalidRegionError(IngestionError, ValueError):
    pass


class SourceClosedError(IngestionError):
    pass


class PixelReadError(IngestionError):
    pass
