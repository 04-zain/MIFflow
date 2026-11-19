"""

Recognizes QuPath saves that preserve the image inputs used by the analysis.

QuPath can rewrite project JSON when an image is opened or display settings change.
Comparing the complete file bytes would treat timestamps, channel colours or notes
as a different analysis. This module removes those documented display fields from
a copy of the project description, then compares the remaining image sources,
channel names, calibration and pipeline assignments.

A small adjacent file associates each previously observed project-file checksum
with a checksum of that reduced description. This allows a later save to be compared
with the project version recorded by the analysis. Stage-specific checks cover
annotations and model files when their workflow steps consume them.
"""

from __future__ import annotations

from collections.abc import Mapping
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile

# ==============================================================================
#                         Project-content projection
# ==============================================================================

_MAX_PROJECT_BYTES = 8 * 1024 * 1024
_MAX_INDEX_BYTES = 2 * 1024 * 1024
_INDEX_SCHEMA = "mif-project-content/v1"


# ==============================================================================
#                                Project content
# ==============================================================================


def _unique_object(pairs):
    """

    Builds a JSON object while rejecting repeated keys that could hide competing project values.

    Args:
        pairs: Key/value pairs from one decoded JSON object.

    Returns:
        A dictionary with unique field names.

    Raises:
        ValueError: A field name is repeated.
    """

    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"The QuPath project repeats the field {key!r}.")
        result[key] = value
    return result


def _decode(payload: bytes) -> dict:
    """

    Decodes a project or comparison-index JSON object and rejects ambiguous fields.

    Duplicate keys and nonfinite numbers are rejected so a single project field has
    one interpretable value during comparison.

    Args:
        payload: JSON bytes read from a size-checked file.

    Returns:
        The decoded dictionary.

    Raises:
        ValueError: The JSON is invalid, repeats fields, contains a nonfinite number,
            or has a top-level value other than an object.
    """

    value = json.loads(
        payload,
        object_pairs_hook=_unique_object,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"The project contains a nonfinite number: {value}")
        ),
    )
    if not isinstance(value, dict):
        raise ValueError("The QuPath project must contain a JSON object.")
    return value


def project_projection(payload: bytes) -> dict:
    """

    Creates the project description used to compare image inputs after a QuPath save.

    Starting from a copy of the JSON, remove creation/modification timestamps,
    randomized display names, descriptions, tags, channel colours and preferred display
    tile sizes. Keep image IDs, reader settings, geometry, calibration, channel names
    and metadata fields beginning with "mif_pipeline.". Sort image entries by ID so
    rearranging the project list preserves the comparison.

    Unknown fields remain in the description, making an unexpected project change
    visible rather than guessing whether it affects analysis.

    Args:
        payload: Contents of the QuPath project file, limited to 8 MiB.

    Returns:
        A dictionary containing the project fields used for analysis comparison.

    Raises:
        ValueError: The file is oversized or the project structure, image IDs or
            image readers cannot be interpreted.
    """

    if not isinstance(payload, bytes) or len(payload) > _MAX_PROJECT_BYTES:
        raise ValueError("The QuPath project exceeds the supported size.")
    project = _decode(payload)
    images = project.get("images")
    if not isinstance(images, list):
        raise ValueError("The QuPath project has no readable image inventory.")

    # Work on a copy so the original project remains available unchanged
    # for native QuPath and for the complete-file checksum recorded elsewhere.
    result = copy.deepcopy(project)
    for key in ("createTimestamp", "modifyTimestamp", "lastID"):
        result.pop(key, None)
    identifiers = set()
    for entry in result["images"]:
        if not isinstance(entry, dict) or "entryID" not in entry:
            raise ValueError("A QuPath image entry is missing its identifier.")
        entry_id = str(entry["entryID"])
        if entry_id in identifiers:
            raise ValueError("The QuPath project repeats an image identifier.")
        identifiers.add(entry_id)
        for key in ("randomizedName", "description", "tags"):
            entry.pop(key, None)
        metadata = entry.get("metadata")
        if isinstance(metadata, Mapping):
            entry["metadata"] = {
                key: value for key, value in metadata.items() if key.startswith("mif_pipeline.")
            }
        builder = entry.get("serverBuilder")
        if not isinstance(builder, dict):
            raise ValueError("A QuPath image entry has no readable image server.")

        # These selected fields describe display or preferred tile sizes.
        # Keep source geometry, calibration and channel names in this same map.
        image_metadata = builder.get("metadata")
        if isinstance(image_metadata, dict):
            for key in ("name", "preferredTileWidth", "preferredTileHeight"):
                image_metadata.pop(key, None)
            for channel in image_metadata.get("channels", []):
                if isinstance(channel, dict):
                    channel.pop("color", None)
    result["images"].sort(key=lambda entry: str(entry["entryID"]))
    return result


def _signature(payload: bytes) -> str:
    """

    Calculates a checksum of the image-input description used in project comparison.

    Sorted keys and compact JSON make the checksum independent of formatting in the
    saved QuPath file.

    Args:
        payload: Project JSON bytes.

    Returns:
        The SHA-256 checksum of project_projection(payload).
    """

    encoded = json.dumps(
        project_projection(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _index_path(project_path: Path) -> Path:
    """

    Locates the comparison record beside the QuPath project.

    Args:
        project_path: Location of the .qpproj file.

    Returns:
        The adjacent filename ending in ".qpproj.mif-content.json".
    """

    return project_path.with_name(project_path.name + ".mif-content.json")


def _read_index(path: Path) -> dict:
    """

    Reads the previously recorded project versions and their image-input checksums.

    Each entry maps a complete project-file checksum to the corresponding reduced
    image-input checksum. An absent index starts with an empty mapping; an existing
    index must have the supported format and remain within the 2 MiB limit.

    Args:
        path: Adjacent comparison-index file to read.

    Returns:
        A dictionary mapping project-file checksums to image-input checksums.

    Raises:
        ValueError: The index is oversized, linked, malformed or uses an unsupported format.
    """

    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_INDEX_BYTES:
        raise ValueError("The project-content index is unavailable or invalid.")
    record = _decode(path.read_bytes())
    if set(record) != {"schema", "snapshots"} or record["schema"] != _INDEX_SCHEMA:
        raise ValueError("The project-content index has an unsupported format.")
    snapshots = record["snapshots"]
    if not isinstance(snapshots, dict) or any(
        not isinstance(key, str) or len(key) != 64 or not isinstance(value, str) or len(value) != 64
        for key, value in snapshots.items()
    ):
        raise ValueError("The project-content index contains an invalid snapshot.")
    return snapshots


# ==============================================================================
#                   Recording and comparing observed snapshots
# ==============================================================================


def remember_project_content(project_path: Path, observed_sha256: str) -> None:
    """

    Records the image-input description corresponding to a known project-file version.

    Read the project and verify that its contents still match the checksum just
    observed by the caller. Then calculate the reduced image-input checksum and add
    that pair to the adjacent index. Reobserving the same pair requires no file write.

    The updated index is written to a temporary file, flushed, and replaced as one
    file operation so readers receive a complete JSON document.

    Args:
        project_path: QuPath project whose current version should be remembered.
        observed_sha256: Checksum obtained when that project version was read.

    Raises:
        ValueError: The project changed during the check, a file exceeds its limit,
            or the saved index disagrees with the observed version.
        OSError: The project or index cannot be read or written.
    """

    project_path = Path(project_path)
    if project_path.is_symlink() or not project_path.is_file():
        raise ValueError("The QuPath project is unavailable.")
    if project_path.stat().st_size > _MAX_PROJECT_BYTES:
        raise ValueError("The QuPath project exceeds the supported size.")
    payload = project_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != observed_sha256:
        raise ValueError("The QuPath project changed during its content check.")
    signature = _signature(payload)
    destination = _index_path(project_path)
    snapshots = _read_index(destination)
    if snapshots.get(observed_sha256) == signature:
        return
    if observed_sha256 in snapshots:
        raise ValueError("The project-content index disagrees with the observed project.")
    snapshots[observed_sha256] = signature
    encoded = json.dumps(
        {"schema": _INDEX_SCHEMA, "snapshots": snapshots},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_INDEX_BYTES:
        raise ValueError("The project-content index exceeds the supported size.")
    descriptor, temporary = tempfile.mkstemp(prefix=".project-content-", dir=project_path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def project_content_matches(project_path: Path, expected_sha256: str) -> bool:
    """

    Checks whether the current project still describes the image inputs of an earlier version.

    An identical file checksum is an immediate match. Otherwise, calculate the current
    image-input checksum and compare it with the saved value for the expected project
    version. That comparison requires the earlier version to have been recorded.

    Args:
        project_path: Current QuPath project file.
        expected_sha256: Project-file checksum recorded by the analysis.

    Returns:
        True when the bytes match or the recorded image-input descriptions agree.
        False when the project is unavailable, oversized, unrecorded or changed.

    Raises:
        ValueError: The project or saved index contains invalid JSON or inconsistent records.
    """

    project_path = Path(project_path)
    if project_path.is_symlink() or not project_path.is_file():
        return False
    if project_path.stat().st_size > _MAX_PROJECT_BYTES:
        return False
    payload = project_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() == expected_sha256:
        return True

    # Skip rewriting the index when this file version already has the same comparison checksum.
    expected = _read_index(_index_path(project_path)).get(expected_sha256)
    return expected is not None and _signature(payload) == expected


def same_project_snapshot(
    project_path: Path,
    expected: str | None,
    observed: str | None,
    *,
    observed_payload: bytes | None = None,
) -> bool:
    """

    Compares a freshly observed project version with the version expected by the analysis.

    Equal checksums satisfy the comparison directly. When they differ, first confirm
    that the current file still matches the new observation, then compare its image
    inputs against the earlier recorded version. This avoids applying a stale observation
    after another project save.

    Args:
        project_path: Project file associated with both observations.
        expected: Earlier project checksum, or None for an absent project.
        observed: Freshly obtained checksum, or None for an absent project.
        observed_payload: Optional bytes from the caller's retained project read.
            The caller keeps that read stable until this comparison completes.

    Returns:
        Whether the observations agree, including an equivalent image-input comparison
        when their file contents differ.
    """

    if expected == observed:
        return True
    if expected is None or observed is None:
        return False
    path = Path(project_path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_PROJECT_BYTES:
        return False
    payload = path.read_bytes() if observed_payload is None else observed_payload
    if len(payload) > _MAX_PROJECT_BYTES or hashlib.sha256(payload).hexdigest() != observed:
        return False
    expected_signature = _read_index(_index_path(path)).get(expected)
    return expected_signature is not None and _signature(payload) == expected_signature
