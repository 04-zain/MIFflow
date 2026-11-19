"""

Provides the current transform reader for registration and annotation transfer.

We use one QuPath helper to read native affine/TPS transforms and the fitted
Elastix B-spline coefficients. Each operation receives that helper's source
and checksum with its request. This keeps the numerical interpretation the
same during preview, registration checks and annotation transfer, including
when the installed Python package stores its scripts as package resources.

"""

from collections.abc import Mapping
import hashlib


def transform_reader_request() -> dict[str, str]:
    """

    Reads the installed transform helper and includes its exact contents in the native request.

    The helper is loaded once when the QuPath operation starts. It then evaluates
    all requested coordinates using the same affine, TPS or B-spline calculation.
    The source text is small relative to image data and is sent once per operation.

    Returns:
        Dictionary containing the UTF-8 Groovy source and its SHA-256 checksum.

    """

    from .scripts import QuPathScriptAsset, _source_bytes

    payload = _source_bytes((QuPathScriptAsset.PAIRED_GEOMETRY,))[0][1]
    return {"source": payload.decode("utf-8"), "sha256": hashlib.sha256(payload).hexdigest()}


def validate_transform_reader_request(value: object) -> None:
    """

    Checks that a registration request uses the current installed transform calculation.

    Compare both source text and checksum with the registered helper. A
    previously recorded operation must be rebuilt if its calculation differs
    from the version now installed; we do not evaluate replacement source supplied
    through an image or annotation file.

    Args:
        value: Transform-reader dictionary included in the current operation.

    Raises:
        ValueError: Fields, text encoding or helper contents differ from the
            registered source used by this installation.

    """

    if not isinstance(value, Mapping) or set(value) != {"source", "sha256"}:
        raise ValueError("The registration request is missing its transform reader.")
    if not all(isinstance(value[name], str) for name in ("source", "sha256")):
        raise ValueError("The transform reader must contain source text and its checksum.")
    if len(value["source"].encode("utf-8")) > 512 * 1024:
        raise ValueError("The registration transform reader exceeds the supported size.")
    if dict(value) != transform_reader_request():
        raise ValueError("The registration tools changed. Reopen the alignment step before continuing.")
