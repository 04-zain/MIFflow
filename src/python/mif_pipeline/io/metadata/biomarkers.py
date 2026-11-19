"""

Converts the prepared-channel review table into channel labels and biomarker assignments.

Each row repeats the source image/channel positions, Channel ID, display name and
fluorophore. Comparing those fields on import detects a table exported before the
image channels changed. The editable component field is a channel label, and the
biomarker field records its biological target. Neither supplies missing dye metadata.

A marker cell has three meanings: named text assigns a marker, the exact token
"None" records an intentional no-marker choice, and a blank cell remains unreviewed.
The shared CSV/UI validators require a completed row for every image channel before
applying the review to OME metadata.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from io import StringIO
from numbers import Integral
from pathlib import Path
import re
from typing import Literal

from ome_types import OME

from ..backends import MetadataMappingError
from .canonical import (
    BiomarkerAssignment,
    ComponentIdentityMode,
    apply_component_identity_inventory,
    canonicalize_ome,
    resolve_component_identity_inventory,
    resolve_channel_metadata_index,
)

BIOMARKER_ASSIGNMENT_FIELDS = (
    "image_index",
    "channel_index",
    "channel_id",
    "channel_name",
    "fluorophore",
    "component_name",
    "biomarker",
)


_CANONICAL_INDEX = re.compile(r"^(?:0|[1-9][0-9]*)$")


# ==============================================================================
#                                   Biomarkers
# ==============================================================================


@dataclass(frozen=True, slots=True)
class BiomarkerAssignmentRow:
    """

    Keeps the source-channel identity beside its editable component and biomarker choices.

    Attributes:
        image_index: Zero-based image position.
        channel_index: Channel position within the image.
        channel_id: Source OME Channel ID.
        channel_name: Source display name used when checking the returned table.
        fluorophore: Source Channel.Fluor value.
        biomarker: Explicit biological target, or None.
        biomarker_assignment: "assigned", "none" or "missing" review state.
        component_name: Reviewed channel label, independent of the recorded dye.
    """

    image_index: int
    channel_index: int
    channel_id: str
    channel_name: str | None
    fluorophore: str | None
    biomarker: str | None
    biomarker_assignment: Literal["assigned", "none", "missing"]
    component_name: str | None = None


def resolve_biomarker_assignment_rows(
    ome: OME,
) -> tuple[BiomarkerAssignmentRow, ...]:
    """

    Builds editable channel-review rows from the current metadata.

    Prefill labels from an existing channel review, or from the source display name,
    recorded dye or Channel ID. Biomarker names and their completion states come from
    their separate channel annotations. We retain missing marker cells so they can be completed during channel review.

    Args:
        ome: Metadata to display or export for channel review.

    Returns:
        One BiomarkerAssignmentRow per channel in image/channel order.
    """

    inventories = {
        image_index: resolve_component_identity_inventory(ome, image_index)
        for image_index in range(len(ome.images))
    }
    rows = []
    for channel in resolve_channel_metadata_index(ome):
        inventory = inventories[channel.image_index]
        component_name = (
            inventory.component_names[channel.channel_index]
            if inventory is not None and inventory.mode is ComponentIdentityMode.CHANNEL_FLUOROPHORES
            else channel.name or channel.fluorophore or channel.channel_id
        )
        rows.append(
            BiomarkerAssignmentRow(
                image_index=channel.image_index,
                channel_index=channel.channel_index,
                channel_id=channel.channel_id,
                channel_name=channel.name,
                fluorophore=channel.fluorophore,
                biomarker=channel.biomarker,
                biomarker_assignment=channel.biomarker_assignment,
                component_name=component_name,
            )
        )
    return tuple(rows)


def apply_biomarker_assignment_rows(
    ome: OME,
    rows: Iterable[BiomarkerAssignmentRow],
) -> OME:
    """

    Validates a complete channel review and applies its component and marker decisions.

    Check source-channel identity, full row coverage and explicit marker states before
    updating an OME copy. The per-channel inventory records labels, while markers
    remain separate annotations and Channel.Fluor retains the source's dye metadata.

    Args:
        ome: Source metadata against which the table was prepared.
        rows: Complete reviewed rows for every source channel.

    Returns:
        OME metadata containing the accepted component and biomarker assignments.

    Raises:
        MetadataMappingError: Rows are incomplete, repeated, outdated or inconsistent.
        MetadataConflictError: A reviewed value conflicts with an existing annotation.
    """

    # Materialize a potentially one-shot iterator once: both validators must
    # inspect the same complete set before any copied metadata is updated.
    row_items = tuple(rows)
    assignments = _validated_biomarker_assignments(ome, row_items)
    from .spectral import resolve_spectral_state

    if any(resolve_spectral_state(ome, i).state == "raw" for i in range(len(ome.images))):
        raise MetadataMappingError("Raw band channels cannot receive output component assignments; review the planned component panel")
    component_updates = _validated_component_updates(
        ome,
        row_items,
    )
    canonical = canonicalize_ome(ome, assignments=assignments)
    for image_index, component_names in component_updates:
        canonical = apply_component_identity_inventory(
            canonical,
            image_index,
            component_names,
            mode=ComponentIdentityMode.CHANNEL_FLUOROPHORES,
            promote_channel_fluorophores=False,
        )
    return canonical


def duplicate_channel_biomarker(ome: OME, rows):
    """Find repeated markers unless true source channels bind them to distinct cycles.

    Editable labels cannot establish cycles. Missing cycles and repetitions within
    one cycle retain the existing uniqueness rule, as do separate source images.
    Rows must already refer to actual image/channel positions in this OME record.
    """
    from .cycles import resolve_channel_cycles

    cycles = {i: resolve_channel_cycles(ome, i) for i in range(len(ome.images))}
    seen = {}
    for row in rows:
        if row.biomarker is None:
            continue
        location = (row.image_index, row.channel_index)
        recorded = cycles[row.image_index]
        cycle = None if recorded is None else recorded[row.channel_index]
        previous = seen.setdefault(row.biomarker, [])
        for earlier, earlier_cycle in previous:
            if (earlier[0] != row.image_index or cycle is None
                    or earlier_cycle is None or cycle == earlier_cycle):
                return row.biomarker, earlier, location
        previous.append((location, cycle))
    return None


def _validated_biomarker_assignments(
    ome: OME,
    rows: tuple[BiomarkerAssignmentRow, ...],
    *,
    source: Path | None = None,
) -> tuple[BiomarkerAssignment, ...]:
    """

    Checks channel coverage and translates completed table rows into marker decisions.

    For each image/channel position, compare the repeated source ID, display name and
    fluorophore against current metadata. Then require an assigned marker or an explicit
    no-marker decision. Named markers may repeat only on channels explicitly bound
    to distinct cycles of the same image; multiple no-marker choices are allowed.

    Args:
        ome: Metadata defining the source channel list.
        rows: Review rows to validate.
        source: Optional CSV filename used to report row-specific errors.

    Returns:
        BiomarkerAssignment records sorted by image/channel position.

    Raises:
        MetadataMappingError: A row is invalid, a source channel is missing, or locations
            or named marker assignments repeat.
    """

    metadata_index = resolve_channel_metadata_index(ome)
    expected = {(channel.image_index, channel.channel_index): channel for channel in metadata_index}
    assignments: dict[tuple[int, int], BiomarkerAssignment] = {}

    for row_number, row in enumerate(rows, start=2):
        image_index = _validated_row_index(
            row.image_index,
            "image_index",
            source=source,
            row_number=row_number,
        )
        channel_index = _validated_row_index(
            row.channel_index,
            "channel_index",
            source=source,
            row_number=row_number,
        )
        location = (image_index, channel_index)
        context = f"image {image_index}, channel {channel_index}"
        if location in assignments:
            raise _mapping_error(
                f"is a duplicate for {context}",
                source=source,
                row_number=row_number,
            )
        channel = expected.get(location)
        if channel is None:
            raise _mapping_error(
                f"references unknown {context}",
                source=source,
                row_number=row_number,
            )

        # Compare source ID, name and fluorophore to detect a table exported before channel
        # changes.
        readable_fields = (
            ("channel ID", row.channel_id, channel.channel_id, False),
            ("channel name", row.channel_name, channel.name, True),
            ("fluorophore", row.fluorophore, channel.fluorophore, True),
        )
        for label, actual, expected_value, optional in readable_fields:
            values_match = (
                (actual or None) == (expected_value or None)
                if optional
                else actual == expected_value
            )
            if not values_match:
                raise _mapping_error(
                    f"has stale {label} for {context}: expected "
                    f"{expected_value!r}, found {actual!r}",
                    source=source,
                    row_number=row_number,
                )

        if row.biomarker_assignment == "missing":
            raise _mapping_error(
                f"is not reviewed for {context}; enter a biomarker or exact None",
                source=source,
                row_number=row_number,
            )
        if row.biomarker_assignment == "none":
            if row.biomarker is not None:
                raise _mapping_error(
                    f"state none requires biomarker=None for {context}",
                    source=source,
                    row_number=row_number,
                )
            biomarker = None
        elif row.biomarker_assignment == "assigned":
            if not isinstance(row.biomarker, str) or not row.biomarker.strip():
                raise _mapping_error(
                    f"state assigned requires a biomarker for {context}",
                    source=source,
                    row_number=row_number,
                )
            biomarker = row.biomarker.strip()
            if biomarker == "None":
                raise _mapping_error(
                    f"uses reserved marker None for {context}",
                    source=source,
                    row_number=row_number,
                )
        else:
            raise _mapping_error(
                f"has invalid state {row.biomarker_assignment!r} for {context}",
                source=source,
                row_number=row_number,
            )

        assignments[location] = BiomarkerAssignment(
            image_index=image_index,
            channel_index=channel_index,
            biomarker=biomarker,
        )

    missing = sorted(set(expected) - set(assignments))
    if missing:
        locations = ", ".join(
            f"image {image_index}, channel {channel_index}"
            for image_index, channel_index in missing
        )
        if source is None:
            raise MetadataMappingError(f"Biomarker assignment is missing {locations}")
        raise MetadataMappingError(f"Biomarker assignment file {source} is missing {locations}")

    duplicate = duplicate_channel_biomarker(ome, assignments.values())
    if duplicate is not None:
        biomarker, first_location, location = duplicate
        raise MetadataMappingError(
            "Biomarker assignment has duplicate biomarker "
            f"{biomarker!r} for image {first_location[0]}, "
            f"channel {first_location[1]} and image {location[0]}, "
            f"channel {location[1]}"
        )

    return tuple(assignments[location] for location in sorted(assignments))


def _validated_component_updates(
    ome: OME,
    rows: tuple[BiomarkerAssignmentRow, ...],
) -> tuple[tuple[int, tuple[str, ...]], ...]:
    """

    Checks component choices and identifies which images need a new component annotation.

    Every reviewed channel requires a label. Source fluorophores remain separate
    immutable facts and need not equal those labels. An existing component review
    must agree with the complete ordered labels. Images already carrying that
    review need no additional annotation.

    Args:
        ome: Metadata containing source fluorophores and existing component reviews.
        rows: Rows already checked for complete image/channel coverage.

    Returns:
        Image positions and ordered component lists that must be added.

    Raises:
        MetadataMappingError: Labels are missing, source dyes contain invalid
            whitespace, or labels conflict with an existing review.
    """

    metadata = resolve_channel_metadata_index(ome)
    by_location = {(row.image_index, row.channel_index): row for row in rows}
    updates: list[tuple[int, tuple[str, ...]]] = []
    for image_index in range(len(ome.images)):
        existing_inventory = resolve_component_identity_inventory(ome, image_index)
        component_names: list[str | None] = []
        for channel in metadata.for_image(image_index):
            row = by_location[channel.image_index, channel.channel_index]
            component = _optional_component_name(
                row.component_name,
                image_index=image_index,
                channel_index=channel.channel_index,
            )
            source_fluor = (
                channel.fluorophore
                if isinstance(channel.fluorophore, str) and channel.fluorophore.strip()
                else None
            )
            if source_fluor is not None and source_fluor != source_fluor.strip():
                raise MetadataMappingError(
                    "Existing Channel.Fluor has leading or trailing whitespace for "
                    f"image {image_index}, channel {channel.channel_index}"
                )
            component_names.append(component)

        if any(value is None for value in component_names):
            raise MetadataMappingError(
                f"Component review for image {image_index} must name every channel"
            )
        complete = tuple(value for value in component_names if value is not None)
        if existing_inventory is not None:
            if (
                existing_inventory.component_names != complete
                or existing_inventory.mode is not ComponentIdentityMode.CHANNEL_FLUOROPHORES
            ):
                raise MetadataMappingError(
                    f"Component review conflicts with the existing immutable inventory "
                    f"for image {image_index}"
                )
            continue
        updates.append((image_index, complete))
    return tuple(updates)


def _optional_component_name(
    value: object,
    *,
    image_index: int,
    channel_index: int,
) -> str | None:
    """

    Reads an editable component cell, trimming supplied text and retaining a missing value.

    Args:
        value: Component text or None.
        image_index: Image position included in an error.
        channel_index: Channel position included in an error.

    Returns:
        The cleaned name, or None for an absent/empty cell.

    Raises:
        MetadataMappingError: Supplied text is whitespace-only, non-text or the reserved "None" token.
    """

    if value is None or value == "":
        return None
    if not isinstance(value, str) or not value.strip():
        raise MetadataMappingError(
            f"Component name must be non-empty text for image {image_index}, "
            f"channel {channel_index}"
        )
    normalized = value.strip()
    if normalized == "None":
        raise MetadataMappingError(
            f"Component name uses reserved None for image {image_index}, "
            f"channel {channel_index}"
        )
    return normalized


def _validated_row_index(
    value: object,
    field: str,
    *,
    source: Path | None,
    row_number: int,
) -> int:
    """

    Checks a structured table's image or channel position as a nonnegative integer.

    Args:
        value: Position to validate; booleans are rejected.
        field: Image/channel field name.
        source: Optional source CSV filename.
        row_number: Displayed CSV row number for diagnostics.

    Returns:
        The integer position.

    Raises:
        MetadataMappingError: The index is negative or is not an integer.
    """

    if not isinstance(value, Integral) or isinstance(value, bool) or value < 0:
        raise _mapping_error(
            f"has invalid {field} {value!r}; expected a non-negative integer",
            source=source,
            row_number=row_number,
        )
    return int(value)


def _mapping_error(
    detail: str,
    *,
    source: Path | None,
    row_number: int,
) -> MetadataMappingError:
    """

    Constructs a table error with the CSV filename and row number when available.

    Args:
        detail: Explanation of the failed check.
        source: Optional CSV filename.
        row_number: Row number to include for CSV input.

    Returns:
        A MetadataMappingError for the caller to raise.
    """

    if source is None:
        return MetadataMappingError(f"Biomarker assignment {detail}")
    return MetadataMappingError(f"Biomarker assignment row {row_number} in {source} {detail}")


def render_biomarker_assignment_csv(ome: OME) -> str:
    """

    Exports one review row per channel, retaining source fields needed to check the completed table.

    The editable marker cell contains its assigned text, "None" for an explicit
    no-marker decision, or a blank for an unreviewed channel. Component names occupy
    their own column so dye identity remains separate from biological target.

    Args:
        ome: Image metadata to export for review.

    Returns:
        CSV text with a header and channel rows in the supported field order.
    """

    output = StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=BIOMARKER_ASSIGNMENT_FIELDS,
        lineterminator="\n",
    )
    writer.writeheader()
    for row in resolve_biomarker_assignment_rows(ome):
        biomarker = ""
        if row.biomarker_assignment == "assigned":
            assert row.biomarker is not None
            biomarker = row.biomarker
        elif row.biomarker_assignment == "none":
            biomarker = "None"
        writer.writerow(
            {
                "image_index": row.image_index,
                "channel_index": row.channel_index,
                "channel_id": row.channel_id,
                "channel_name": row.channel_name or "",
                "fluorophore": row.fluorophore or "",
                "component_name": row.component_name or "",
                "biomarker": biomarker,
            }
        )
    return output.getvalue()


def read_biomarker_assignment_csv(
    csv_path: str | Path,
) -> tuple[BiomarkerAssignmentRow, ...]:
    """

    Reads channel-review CSV rows into the structured representation used by the UI.

    Blank marker cells retain the "missing" state so incomplete reviews can be displayed
    and completed before application.

    Args:
        csv_path: UTF-8 review CSV file.

    Returns:
        BiomarkerAssignmentRow records in file order.

    Raises:
        MetadataMappingError: CSV structure, indices or review tokens are invalid.
        OSError, UnicodeError: The file cannot be read as UTF-8 text.
    """

    path = Path(csv_path)
    return _parse_biomarker_assignment_csv(
        path.read_text(encoding="utf-8"),
        source=path,
    )


def _parse_biomarker_assignment_csv(
    text: str,
    *,
    source: Path,
) -> tuple[BiomarkerAssignmentRow, ...]:
    """

    Parses CSV columns and preserves the meaning of each biomarker-review cell.

    Require the supported header and a consistent field count. Parse image/channel
    indices as nonnegative decimal integers. A blank marker cell means review is still
    needed; exact "None" means no marker; other nonblank text names the target.

    We read the component_name field explicitly for every channel. When the
    completed rows are applied, their source IDs and original display/fluorophore
    values are compared with the selected image, followed by the component and
    biomarker decisions.

    Args:
        text: Complete CSV contents.
        source: Filename included in parsing errors.

    Returns:
        Parsed BiomarkerAssignmentRow records in file order.

    Raises:
        MetadataMappingError: Headers, row widths, indices or marker tokens are invalid.
    """

    reader = csv.DictReader(StringIO(text, newline=""))
    try:
        fieldnames = tuple(reader.fieldnames or ())
        parsed_rows = list(reader)
    except csv.Error as exc:
        raise MetadataMappingError(
            f"Invalid biomarker assignment CSV syntax in {source} near "
            f"row {max(reader.line_num, 1)}: {exc}"
        ) from exc

    if fieldnames != BIOMARKER_ASSIGNMENT_FIELDS:
        raise MetadataMappingError(
            f"Invalid biomarker assignment header in {source}; expected "
            + ",".join(BIOMARKER_ASSIGNMENT_FIELDS)
        )

    rows = []

    for row_number, row in enumerate(parsed_rows, start=2):
        if None in row or any(row.get(field) is None for field in fieldnames):
            raise MetadataMappingError(
                f"Biomarker assignment row {row_number} in {source} has the "
                f"wrong number of columns"
            )
        image_index = _parse_index(
            row["image_index"],
            "image_index",
            row_number,
            source,
        )
        channel_index = _parse_index(
            row["channel_index"],
            "channel_index",
            row_number,
            source,
        )

        # Keep a blank cell distinct from the deliberate, exact "None" choice.
        # Ordinary marker names are trimmed only after checking that sentinel.
        raw_biomarker = row["biomarker"]
        stripped = raw_biomarker.strip()
        if not stripped:
            biomarker = None
            state = "missing"
        elif raw_biomarker == "None":
            biomarker = None
            state = "none"
        elif stripped == "None":
            raise MetadataMappingError(
                f"Biomarker assignment row {row_number} in {source} must use "
                "the exact sentinel None"
            )
        else:
            biomarker = stripped
            state = "assigned"
        rows.append(
            BiomarkerAssignmentRow(
                image_index=image_index,
                channel_index=channel_index,
                channel_id=row["channel_id"],
                channel_name=row["channel_name"] or None,
                fluorophore=row["fluorophore"] or None,
                biomarker=biomarker,
                biomarker_assignment=state,
                component_name=row["component_name"] or None,
            )
        )

    return tuple(rows)


def _parse_index(
    value: str,
    field: str,
    row_number: int,
    source: Path,
) -> int:
    """

    Parses a CSV position written as a plain nonnegative decimal integer.

    The accepted form uses "0", "1", "23" and similar text. Signs, leading zeros,
    whitespace and decimal points are rejected so exported and returned positions
    have one representation.

    Args:
        value: Exact CSV index text.
        field: Index column name.
        row_number: CSV row number, including the header as row 1.
        source: Filename used in diagnostics.

    Returns:
        The parsed image/channel position.

    Raises:
        MetadataMappingError: The text is outside the supported index format.
    """

    if _CANONICAL_INDEX.fullmatch(value) is None:
        raise MetadataMappingError(
            f"Biomarker assignment row {row_number} in {source} has invalid " f"{field}: {value!r}"
        )
    return int(value)
