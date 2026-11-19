"""

Opens source images through the configured Bio-Formats reader.

The opened reader provides image-series descriptions, OME metadata and
rectangular pixel reads. Callers first inspect those descriptions and
select the analytical image, then keep the reader open while its regions
are needed. This entry point does not start an image conversion.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from pathlib import Path

from .backends import BioFormatsBackend, BioFormatsSource
from .backends.bioformats import _BffileBackend

# ==============================================================================
#                                   Ingestion
# ==============================================================================


def get_default_backend() -> BioFormatsBackend:
    """

    Creates the supported bffile/Bio-Formats backend used for source image reading.

    Returns:
        BioFormatsBackend instance that opens files using the selected local
        Java and Bio-Formats installation.
    """

    return _BffileBackend()


def open_image(
    path: str | Path,
    *,
    backend: BioFormatsBackend | None = None,
) -> BioFormatsSource:
    """

    Opens an image file for metadata inspection and subsequent regional pixel reads.

    A supplied backend allows callers to use a specific initialized reader
    implementation. Otherwise the function creates the default Bio-Formats
    backend. The returned source must remain open while its regions are being
    read and should be closed by the calling workflow afterward.

    Args:
        path: Source image filename, including supported vendor-image formats.
        backend: Optional BioFormatsBackend used to interpret and open the file.

    Returns:
        BioFormatsSource exposing the reader's series, OME metadata and
        read_region() operation.
    """

    selected = backend if backend is not None else get_default_backend()
    return selected.open(Path(path))
