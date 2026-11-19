"""

Selects the analytical image, retains its reviewed metadata and supports explicit standardized copies.

A vendor file can contain the full tissue image alongside a label, overview
or thumbnail. The reader first lists these image-series pairs and checks
which ones support the requested analysis. A single eligible pair can be
selected automatically; multiple eligible pairs require a recorded choice.
Selecting a pair produces a one-image OME view while retaining the original
vendor-series index for later regional reads.

The inspection record stores the source file identity, selected image,
reader versions, pyramid dimensions and OME XML. Channel and acquisition
review can then operate on that metadata while the source reader is closed.
Direct vendor-image unmixing reopens the selected source with these reviewed
settings and writes the standardized component image from its fitted regions.

This module also exposes pixel-preserving sharded image publication.
The source adapter reads each native level in bounded regions, and the writer
publishes one immutable NGFF 0.5/Zarr 3 generation with the reviewed OME meanings.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal

import numpy as np
from ome_types import OME, from_xml, to_xml

from .backends import (
    BackendVersions,
    BioFormatsBackend,
    MetadataCanonicalizationError,
    OpenedSourceIdentity,
    ResolutionInfo,
    SeriesInfo,
    SourceStorageBits,
)
from .ingestion import get_default_backend, open_image
from .metadata.biomarkers import (
    apply_biomarker_assignment_rows,
    resolve_biomarker_assignment_rows,
)
from .metadata.canonical import (
    CHANNEL_METADATA_NAMESPACE,
    COMPONENT_IDENTITY_NAMESPACE,
    ComponentIdentityMode,
    resolve_component_identity_inventory,
    resolve_unmixing_input_metadata,
    select_image_ome_metadata,
)

# ==============================================================================
#                      Source inspection and image selection
# ==============================================================================


class CanonicalIngestionError(RuntimeError):
    """

    Reports an ambiguous source selection or a standardized copy that fails its image checks.
    """

@dataclass(frozen=True, slots=True)
class CanonicalImageSelection:
    """

    Identifies the exact OME image and native reader series selected from a source file.

    OME image positions and native reader-series positions are retained
    separately because metadata is later presented as a single image at
    position zero. The original series index is still needed to read the
    correct vendor-image pixels.

    Attributes:
        source_image_index: Zero-based position in the source OME image list.
        source_series_index: Native Bio-Formats series index used for pixel reads.
        image_id: Original OME Image ID.
        pixels_id: Original Pixels ID for that image.
        series_name: Literal source-series name, including an empty name when absent.
    """

    source_image_index: int
    source_series_index: int
    image_id: str
    pixels_id: str
    series_name: str


SourceSeriesEligibilityReason = Literal[
    "eligible_analytical_series",
    "requires_cycle_organization",
    "missing_resolution_inventory",
    "unaligned_image_series_index",
    "associated_thumbnail",
    "non_planar_channels",
    "unsupported_z_or_time",
    "dimension_mismatch",
    "channel_layout_mismatch",
    "pixel_type_mismatch",
]


@dataclass(frozen=True, slots=True)
class SourceSeriesCandidate:
    """

    Presents one inspected image-series pair and the reasons it is eligible or ineligible.

    Display names provide usable labels in the selection form. They are kept
    separate from the literal source identifiers used to reopen the chosen
    image, so a fallback display label cannot become an invented source name.

    Attributes:
        candidate_id: Checksum-derived identity of this source file and selection.
        source_image_index: Position of the OME image in the source.
        source_series_index: Matching native Bio-Formats series index.
        image_id: OME Image ID used in the selection comparison.
        pixels_id: OME Pixels ID used in the selection comparison.
        image_display_name: Image name, falling back to its ID when unnamed.
        series_display_name: Series name or a generated display label.
        size_x, size_y, size_c: Full-resolution dimensions and channel count.
        eligibility: "eligible" or "ineligible" for the requested channel layout.
        reasons: Reasons such as thumbnail status, incompatible dimensions,
            unsupported Z/time positions or a channel/dtype mismatch.
    """

    candidate_id: str
    source_image_index: int
    source_series_index: int
    image_id: str
    pixels_id: str
    image_display_name: str
    series_display_name: str
    eligibility: Literal["eligible", "needs_organization", "ineligible"]
    reasons: tuple[SourceSeriesEligibilityReason, ...]
    size_x: int | None = None
    size_y: int | None = None
    size_c: int | None = None


@dataclass(frozen=True, slots=True)
class SourceSeriesInventory:
    """

    Retains the inspected image choices after their source reader has been closed.

    The saved checksum covers the source identity, reader versions, displayed
    candidates and literal selections. A selected candidate can therefore be
    matched back to this exact inspection rather than another file's similarly
    named image. The later source opening still checks that the source file
    agrees with this retained inspection.

    Attributes:
        schema_name: Fixed record name "mif-source-series-inventory".
        schema_version: Current record version, 1.
        source_identity: Source path, file checksum and recorded filesystem facts.
        candidates: Ordered SourceSeriesCandidate descriptions shown during selection.
        backend_versions: Image-reader versions used during inspection.
        inventory_sha256: Checksum of the complete inspection-choice description.
        _selections: Literal CanonicalImageSelection records corresponding to
            the displayed candidates in the same order.
    """

    schema_name: Literal["mif-source-series-inventory"]
    schema_version: Literal[1]
    source_identity: OpenedSourceIdentity
    candidates: tuple[SourceSeriesCandidate, ...]
    backend_versions: BackendVersions
    inventory_sha256: str
    _selections: tuple[CanonicalImageSelection, ...] = field(repr=False)


def inspect_source_series_inventory(
    source_path: Path,
    *,
    require_planar_channels: bool = True,
    allow_pending_organization: bool = False,
) -> SourceSeriesInventory:
    """

    Lists each image-series pair and checks its suitability for the requested analysis.

    Open the source once, obtain OME and reader descriptions, then compare each
    matching image/series position. The checks distinguish analytical images
    from associated thumbnails and report incompatible Z/time dimensions,
    channel organization and dtype. The returned record retains both eligible
    and ineligible choices with their reasons.

    Args:
        source_path: Vendor or standardized source filename to inspect.
        require_planar_channels: Whether each channel must be a separate scalar
            plane. mIF processing enables this requirement; other image-selection
            uses can admit an analytical RGB image.

    Returns:
        SourceSeriesInventory with the source identity, candidates, literal
        selections and reader versions. The reader is closed before returning.

    Raises:
        CanonicalIngestionError: OME images and native series cannot be aligned,
            source/reader identities are unavailable or the layout requirement
            is not a boolean.
    """

    if not isinstance(require_planar_channels, bool):
        raise CanonicalIngestionError("planar-channel requirement must be boolean")
    with open_image(source_path) as source:
        ome = source.metadata
        series = tuple(source.series)
        identity = source.identity
        versions = source.versions
        if not isinstance(ome, OME) or len(ome.images) != len(series):
            raise CanonicalIngestionError(
                "OME image and Bio-Formats series inventories must have equal length"
            )
        if not isinstance(identity, OpenedSourceIdentity) or not isinstance(
            versions, BackendVersions
        ):
            raise CanonicalIngestionError(
                "source inventory requires IO-owned identity and versions"
            )
        candidates = []
        selections = []
        for index, descriptor in enumerate(series):
            image = ome.images[index]
            selection = CanonicalImageSelection(
                source_image_index=index,
                source_series_index=descriptor.index,
                image_id=image.id,
                pixels_id=image.pixels.id,
                series_name=descriptor.name,
            )
            reasons = _analytical_pair_reasons(
                ome,
                index,
                descriptor,
                require_planar_channels=require_planar_channels,
            )
            if allow_pending_organization and require_planar_channels and reasons == ("unsupported_z_or_time",):
                from .acquired_plane_view import validate_source_dimensions
                try:
                    validate_source_dimensions(ome, index, descriptor)
                except ValueError:
                    pass
                else:
                    reasons = ("requires_cycle_organization",)
            candidates.append(
                SourceSeriesCandidate(
                    candidate_id=_source_series_candidate_id(identity, selection),
                    source_image_index=index,
                    source_series_index=descriptor.index,
                    image_id=image.id,
                    pixels_id=image.pixels.id,
                    image_display_name=image.name or image.id,
                    series_display_name=descriptor.name or f"Series:{descriptor.index}",
                    eligibility=(
                        "eligible" if reasons == ("eligible_analytical_series",) else
                        "needs_organization" if reasons == ("requires_cycle_organization",) else "ineligible"
                    ),
                    reasons=reasons,
                    size_x=image.pixels.size_x,
                    size_y=image.pixels.size_y,
                    size_c=image.pixels.size_c,
                )
            )
            selections.append(selection)
        inventory = SourceSeriesInventory(
            schema_name="mif-source-series-inventory",
            schema_version=1,
            source_identity=identity,
            candidates=tuple(candidates),
            backend_versions=versions,
            inventory_sha256="",
            _selections=tuple(selections),
        )
        return replace(
            inventory, inventory_sha256=_json_sha256(_source_series_inventory_mapping(inventory))
        )


def bind_source_series_selection(
    inventory: SourceSeriesInventory,
    *,
    candidate_id: str,
    allow_pending_organization: bool = False,
) -> CanonicalImageSelection:
    """

    Resolves the researcher's candidate choice against the retained source inspection.

    Recalculate the inspection checksum, then find the requested candidate and
    compare its image/series IDs with the corresponding literal selection.
    Only a candidate recorded as eligible can become the analytical selection.
    Pixel reads are deferred until the selected source is reopened by its workflow.

    Args:
        inventory: SourceSeriesInventory returned by source inspection.
        candidate_id: Candidate identifier submitted by the selection form.

    Returns:
        CanonicalImageSelection for the eligible candidate.

    Raises:
        CanonicalIngestionError: The inspection changed, candidate identities
            disagree, or the selected candidate is absent or ineligible.
    """

    if not isinstance(inventory, SourceSeriesInventory):
        raise CanonicalIngestionError("source series inventory has an invalid type")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise CanonicalIngestionError("source series selection requires a candidate ID")
    try:
        valid = (
            inventory.schema_name == "mif-source-series-inventory"
            and type(inventory.schema_version) is int
            and inventory.schema_version == 1
            and isinstance(inventory.source_identity, OpenedSourceIdentity)
            and isinstance(inventory.backend_versions, BackendVersions)
            and isinstance(inventory.candidates, tuple)
            and isinstance(inventory._selections, tuple)
            and len(inventory.candidates) == len(inventory._selections)
            and inventory.inventory_sha256
            == _json_sha256(_source_series_inventory_mapping(inventory))
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise CanonicalIngestionError("source series inventory is invalid") from exc
    if not valid:
        raise CanonicalIngestionError("source series inventory is stale or invalid")
    for candidate, selection in zip(inventory.candidates, inventory._selections, strict=True):
        if candidate.candidate_id != candidate_id:
            continue
        if candidate.candidate_id != _source_series_candidate_id(
            inventory.source_identity, selection
        ) or (
            candidate.source_image_index,
            candidate.source_series_index,
            candidate.image_id,
            candidate.pixels_id,
        ) != (
            selection.source_image_index,
            selection.source_series_index,
            selection.image_id,
            selection.pixels_id,
        ):
            raise CanonicalIngestionError("source series candidate identity is stale")
        if not ((candidate.eligibility == "eligible" and candidate.reasons == ("eligible_analytical_series",))
                or (allow_pending_organization and candidate.eligibility == "needs_organization"
                    and candidate.reasons == ("requires_cycle_organization",))):
            raise CanonicalIngestionError("source series candidate is ineligible")
        return selection
    raise CanonicalIngestionError("source series candidate is foreign to this inventory")


def _source_series_candidate_id(
    identity: OpenedSourceIdentity,
    selection: CanonicalImageSelection,
) -> str:
    """

    Calculates an identifier for one source file and its exact image-series selection.

    Args:
        identity: Source file identity recorded during the reader opening.
        selection: Literal image/series identifiers to associate with that file.

    Returns:
        SHA-256 of their deterministic JSON representation.
    """

    return _json_sha256(
        {
            "source_identity": _source_identity_mapping(identity),
            "selection": _selection_mapping(selection),
        }
    )


def _source_series_inventory_mapping(inventory: SourceSeriesInventory) -> dict[str, object]:
    """

    Collects the complete inspected source choices into a serializable dictionary.

    Displayed candidates and their literal selections are both included so
    the checksum covers the selection information actually shown and resolved.

    Args:
        inventory: Inspected source-choice record.

    Returns:
        Dictionary containing the record version, source, reader versions,
        candidate descriptions and corresponding literal selections.
    """

    return {
        "schema_name": inventory.schema_name,
        "schema_version": inventory.schema_version,
        "source_identity": _source_identity_mapping(inventory.source_identity),
        "backend_versions": _backend_versions_mapping(inventory.backend_versions),
        "candidates": [
            {
                name: getattr(candidate, name)
                for name in (
                    "candidate_id",
                    "source_image_index",
                    "source_series_index",
                    "image_id",
                    "pixels_id",
                    "image_display_name",
                    "series_display_name",
                    "eligibility",
                    "reasons",
                    "size_x",
                    "size_y",
                    "size_c",
                )
            }
            for candidate in inventory.candidates
        ],
        "selections": [_selection_mapping(selection) for selection in inventory._selections],
    }


@dataclass(frozen=True, slots=True)
class CanonicalPublicationResult:
    """

    Describes a newly written or verified reused standardized image and its conversion record.

    Attributes:
        status: "published" for a completed conversion or "reused" for a
            matching previously completed output.
        source_path: Source file from which the selected image was copied.
        output_path: Selected immutable OME-Zarr image directory.
        provenance_path: Adjacent .ingestion.json conversion record.
        source_sha256: Source content checksum associated with the conversion.
        output_sha256: Completed root metadata digest identifying this generation.
        selection: Exact analytical image/series selected from the source.
        pyramid_levels: Number of native levels preserved in the output.
    """

    status: str
    source_path: Path
    output_path: Path
    provenance_path: Path
    source_sha256: str
    output_sha256: str
    selection: CanonicalImageSelection
    pyramid_levels: int


# ==============================================================================
#             Retaining a source inspection for review and reopening            
# ==============================================================================


@dataclass(frozen=True, slots=True)
class InspectedSourceAuthority:
    """

    Stores the source-image inspection needed for metadata review and later regional processing.

    The record keeps OME as serialized XML so its contents are independent of
    later edits to a mutable OME model. Reopening canonical_ome parses a fresh
    metadata model from that saved XML. The content checksum covers the file
    identity, XML, series descriptions, selected image and reader versions.

    Here the name "authority" refers to the recorded inspection used to check
    later inputs. It neither opens an image by itself nor implies that pixel
    conversion occurred during inspection.

    Attributes:
        schema_name: Fixed name "mif-inspected-source-authority".
        schema_version: Current inspection-record version, 2.
        source_identity: Original file path, content checksum and filesystem facts.
        canonical_ome_xml: Shared OME metadata serialized during inspection.
        series: Ordered native-series descriptions, including all stored levels.
        selection: Analytical image/series retained for later processing.
        backend_versions: Reader versions used to interpret the source.
        content_sha256: Checksum of this record's complete serialized content.
    """

    schema_name: str
    schema_version: int
    source_identity: OpenedSourceIdentity
    canonical_ome_xml: str
    series: tuple[SeriesInfo, ...]
    selection: CanonicalImageSelection
    backend_versions: BackendVersions
    content_sha256: str

    def __post_init__(self) -> None:
        """

        Checks the inspection format, saved OME XML and internal source-selection relationships.

        We admit typed, immutable series, selection and reader facts here so later
        source opening can reuse this record. Series indices must be consecutive
        from zero and the selected series must exist. The saved content checksum
        must match the complete record before downstream code can reuse its
        metadata and image choice.

        Raises:
            TypeError: File identity, series, selection or reader-version records
                have an incompatible type.
            ValueError: XML, schema, series order, selected index or content checksum
                is invalid.
        """

        if type(self.schema_version) is not int or (self.schema_name, self.schema_version) != (
            "mif-inspected-source-authority",
            2,
        ):
            raise ValueError("inspected source authority schema is unsupported")
        if not isinstance(self.source_identity, OpenedSourceIdentity):
            raise TypeError("inspected source authority requires source identity")
        if not isinstance(self.canonical_ome_xml, str) or not self.canonical_ome_xml:
            raise ValueError("inspected source authority requires canonical OME XML")
        try:
            from_xml(self.canonical_ome_xml)
        except Exception as exc:
            raise ValueError("inspected source authority OME XML is invalid") from exc
        values = tuple(self.series)
        if not values or not all(isinstance(item, SeriesInfo) for item in values):
            raise TypeError("inspected source authority requires a series inventory")
        if tuple(item.index for item in values) != tuple(range(len(values))):
            raise ValueError("inspected source series indices are not canonical")
        object.__setattr__(self, "series", values)
        if not isinstance(self.selection, CanonicalImageSelection):
            raise TypeError("inspected source authority selection is invalid")
        selected_index = self.selection.source_series_index
        if type(selected_index) is not int or not 0 <= selected_index < len(values):
            raise ValueError("inspected selected-series index is invalid")
        if not isinstance(self.backend_versions, BackendVersions):
            raise TypeError("inspected source authority backend versions are invalid")
        selection = self.selection
        if not (
            type(selection.source_image_index) is int
            and type(selection.source_series_index) is int
            and isinstance(selection.image_id, str)
            and bool(selection.image_id)
            and isinstance(selection.pixels_id, str)
            and bool(selection.pixels_id)
            and isinstance(selection.series_name, str)
        ):
            raise ValueError("inspected source selection fields are invalid")
        versions = self.backend_versions
        if not all(
            isinstance(getattr(versions, field), str) and bool(getattr(versions, field))
            for field in (
                "backend",
                "bffile_version",
                "bioformats_maven_coordinate",
                "bioformats_runtime_version",
            )
        ):
            raise ValueError("inspected backend versions are invalid")
        for series in self.series:
            if not isinstance(series.name, str):
                raise ValueError("inspected series name is invalid")
            for level in series.resolutions:
                if not isinstance(level, ResolutionInfo):
                    raise TypeError("inspected resolution must be a native resolution record")
                if not isinstance(level.dimension_order, str) or type(level.is_thumbnail) is not bool:
                    raise ValueError("inspected resolution fields are invalid")

        if (
            not isinstance(self.content_sha256, str)
            or len(self.content_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.content_sha256)
            or self.content_sha256 != _json_sha256(self._content_mapping())
        ):
            raise ValueError("inspected source authority content digest is stale")

    @property
    def canonical_ome(self) -> OME:
        """

        Parses a fresh OME metadata model from the XML retained during inspection.

        Returns:
            OME model that callers can copy and review without modifying the
            inspection record's saved XML.
        """

        return from_xml(self.canonical_ome_xml)

    @property
    def selected_storage_bits(self) -> SourceStorageBits | None:
        """

        Returns the stored-bit evidence recorded for the selected native series.

        Returns:
            SourceStorageBits when the reader supplied actual storage-width evidence,
            otherwise None. The later range resolver also considers OME SignificantBits.
        """

        return self.series[self.selection.source_series_index].storage_bits

    @classmethod
    def create(
        cls,
        *,
        source_identity: OpenedSourceIdentity,
        canonical_ome: OME,
        series: tuple[SeriesInfo, ...],
        selection: CanonicalImageSelection,
        backend_versions: BackendVersions,
    ) -> "InspectedSourceAuthority":
        """

        Captures the inspected metadata and reader facts in a checksum-bound source record.

        Serialize the supplied OME model and combine it with the original file,
        series, selection and reader descriptions. The checksum is calculated from
        that complete representation before constructing the checked record.

        Args:
            source_identity: Identity captured while the source reader was open.
            canonical_ome: Shared OME model produced by source inspection.
            series: Native image-series descriptions in source order.
            selection: Exact image/series chosen for analysis.
            backend_versions: Versions of the reader used during this inspection.

        Returns:
            InspectedSourceAuthority containing the saved metadata and selection.

        Raises:
            TypeError or ValueError: Input types or the resulting record fail their
                XML, series, identity or checksum checks.
        """

        if not isinstance(canonical_ome, OME):
            raise TypeError("inspected source authority requires canonical OME")
        unsigned = {
            "schema_name": "mif-inspected-source-authority",
            "schema_version": 2,
            "source_identity": _source_identity_mapping(source_identity),
            "canonical_ome_xml": to_xml(canonical_ome),
            "series": [_series_mapping(item) for item in series],
            "selection": _selection_mapping(selection),
            "backend_versions": _backend_versions_mapping(backend_versions),
        }
        return cls(
            schema_name="mif-inspected-source-authority",
            schema_version=2,
            source_identity=source_identity,
            canonical_ome_xml=unsigned["canonical_ome_xml"],
            series=tuple(series),
            selection=selection,
            backend_versions=backend_versions,
            content_sha256=_json_sha256(unsigned),
        )

    def _content_mapping(self) -> dict[str, object]:
        """

        Collects the inspection fields used to calculate its content checksum.

        Returns:
            Dictionary containing the record version, file identity, OME XML,
            series descriptions, selected image and reader versions.
        """

        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "source_identity": _source_identity_mapping(self.source_identity),
            "canonical_ome_xml": self.canonical_ome_xml,
            "series": [_series_mapping(item) for item in self.series],
            "selection": _selection_mapping(self.selection),
            "backend_versions": _backend_versions_mapping(self.backend_versions),
        }

    def to_mapping(self) -> dict[str, object]:
        """

        Returns the serialized inspection fields together with their stored checksum.

        Returns:
            Dictionary ready for deterministic JSON encoding.
        """

        return {**self._content_mapping(), "content_sha256": self.content_sha256}

    def to_json_bytes(self) -> bytes:
        """

        Encodes the inspection as UTF-8 JSON with sorted keys and fixed compact separators.

        Returns:
            Bytes used to save and later verify this inspection record.
        """

        return _canonical_json_bytes(self.to_mapping())

    @classmethod
    def from_json_bytes(cls, data: bytes) -> "InspectedSourceAuthority":
        """

        Reconstructs an inspection record and checks its complete saved representation.

        Decode JSON with duplicate-field and nonfinite-number rejection. The byte
        sequence must match the program's deterministic JSON rendering. Check the
        expected nested fields before rebuilding file identity, source series,
        image selection and reader-version records. Construction then rechecks the
        OME XML, selected series and complete content checksum.

        Args:
            data: Exact UTF-8 bytes of the saved inspection record.

        Returns:
            InspectedSourceAuthority ready for source review or reopening.

        Raises:
            TypeError: data is not bytes or a nested record has an incompatible type.
            ValueError: JSON formatting, fields, version, XML, selection or checksum
                differs from the supported inspection record.
        """

        if type(data) is not bytes:
            raise TypeError("inspected source authority payload must be bytes")

        def exact_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            """

            Builds a decoded JSON object while rejecting repeated field names.

            Args:
                pairs: Key/value pairs supplied by the JSON decoder for the current object.

            Returns:
                Dictionary containing each field once.

            Raises:
                ValueError: A key repeats, which would otherwise overwrite an earlier value.
            """

            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate inspected source field {key!r}")
                result[key] = value
            return result

        try:
            value = json.loads(
                data.decode("utf-8"),
                object_pairs_hook=exact_object,
                parse_constant=lambda item: (_ for _ in ()).throw(
                    ValueError(f"nonfinite inspected source value {item!r}")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("inspected source authority is invalid JSON") from exc
        if not isinstance(value, dict) or _canonical_json_bytes(value) != data:
            raise ValueError("inspected source authority bytes are not canonical")
        expected = {
            "schema_name",
            "schema_version",
            "source_identity",
            "canonical_ome_xml",
            "series",
            "selection",
            "backend_versions",
            "content_sha256",
        }
        if set(value) != expected:
            raise ValueError("inspected source authority fields differ")
        if type(value["schema_version"]) is not int or value["schema_version"] != 2:
            raise ValueError("inspected source authority schema version is unsupported")
        source = value["source_identity"]
        selection = value["selection"]
        versions = value["backend_versions"]
        raw_series = value["series"]
        if not isinstance(source, dict) or set(source) != {
            "resolved_path",
            "file_sha256",
            "size_bytes",
            "modified_time_ns",
            "device_id",
            "inode_id",
        }:
            raise ValueError("inspected source identity fields differ")
        if not isinstance(selection, dict) or set(selection) != {
            "source_image_index",
            "source_series_index",
            "image_id",
            "pixels_id",
            "series_name",
        }:
            raise ValueError("inspected source selection fields differ")
        if not isinstance(versions, dict) or set(versions) != {
            "backend",
            "bffile_version",
            "bioformats_maven_coordinate",
            "bioformats_runtime_version",
        }:
            raise ValueError("inspected source backend-version fields differ")
        if not isinstance(raw_series, list):
            raise ValueError("inspected source series inventory is invalid")
        return cls(
            schema_name=value["schema_name"],
            schema_version=value["schema_version"],
            source_identity=OpenedSourceIdentity(
                resolved_path=Path(source["resolved_path"]),
                file_sha256=source["file_sha256"],
                size_bytes=source["size_bytes"],
                modified_time_ns=source["modified_time_ns"],
                device_id=source["device_id"],
                inode_id=source["inode_id"],
            ),
            canonical_ome_xml=value["canonical_ome_xml"],
            series=tuple(_series_from_mapping(item) for item in raw_series),
            selection=CanonicalImageSelection(**selection),
            backend_versions=BackendVersions(**versions),
            content_sha256=value["content_sha256"],
        )


def _canonical_json_bytes(value: object) -> bytes:
    """

    Encodes a record as deterministic UTF-8 JSON for saving and checksum comparison.

    Dictionary keys are sorted, separators are fixed and NaN/infinity are
    rejected. This gives the same byte representation for equivalent field
    values regardless of the dictionary's insertion order.

    Args:
        value: JSON-serializable source or selection record.

    Returns:
        Compact UTF-8 JSON bytes.

    Raises:
        ValueError: A value cannot be represented by the supported finite JSON format.
    """

    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("inspected source authority is not canonical JSON") from exc


def _json_sha256(value: object) -> str:
    """

    Calculates the checksum of a record's deterministic JSON representation.

    Args:
        value: Record containing the identities and settings to associate.

    Returns:
        Lowercase SHA-256 text derived from _canonical_json_bytes().
    """

    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _source_identity_mapping(value: OpenedSourceIdentity) -> dict[str, object]:
    """

    Collects a source file's saved path, content checksum and filesystem facts.

    Args:
        value: OpenedSourceIdentity captured by the reader.

    Returns:
        Serializable dictionary of path, checksum, byte size, modification time,
        device ID and file ID.

    Raises:
        TypeError: value is not an OpenedSourceIdentity.
    """

    if not isinstance(value, OpenedSourceIdentity):
        raise TypeError("source identity is invalid")
    return {
        "resolved_path": str(value.resolved_path),
        "file_sha256": value.file_sha256,
        "size_bytes": value.size_bytes,
        "modified_time_ns": value.modified_time_ns,
        "device_id": value.device_id,
        "inode_id": value.inode_id,
    }


def _selection_mapping(value: CanonicalImageSelection) -> dict[str, object]:
    """

    Collects the original image and native-series identifiers used by a selection.

    Args:
        value: CanonicalImageSelection returned by source selection.

    Returns:
        Dictionary containing both source indices and their original Image,
        Pixels and series identifiers.

    Raises:
        TypeError: value is not a CanonicalImageSelection.
    """

    if not isinstance(value, CanonicalImageSelection):
        raise TypeError("canonical image selection is invalid")
    return {
        "source_image_index": value.source_image_index,
        "source_series_index": value.source_series_index,
        "image_id": value.image_id,
        "pixels_id": value.pixels_id,
        "series_name": value.series_name,
    }


def _backend_versions_mapping(value: BackendVersions) -> dict[str, object]:
    """

    Serializes the reader versions used to interpret a source image.

    Args:
        value: BackendVersions recorded during source inspection.

    Returns:
        Dictionary containing backend, bffile and Bio-Formats dependency/runtime versions.

    Raises:
        TypeError: value is not a BackendVersions record.
    """

    if not isinstance(value, BackendVersions):
        raise TypeError("backend versions are invalid")
    return {
        "backend": value.backend,
        "bffile_version": value.bffile_version,
        "bioformats_maven_coordinate": value.bioformats_maven_coordinate,
        "bioformats_runtime_version": value.bioformats_runtime_version,
    }


def _series_mapping(value: SeriesInfo) -> dict[str, object]:
    """

    Serializes a native image series and each of its stored resolution levels.

    Every level retains X/Y/Z/C/T dimensions, RGB sample count, dtype,
    dimension order and the reader's thumbnail flag. Actual stored-bit
    information remains separate from the decoder's NumPy dtype.

    Args:
        value: SeriesInfo returned by the source reader.

    Returns:
        Dictionary containing the series identity, ordered levels and optional
        stored-bit evidence.
    """

    return {
        "index": value.index,
        "name": value.name,
        "storage_bits": (
            None
            if value.storage_bits is None
            else {
                "bits": value.storage_bits.bits,
                "origin": value.storage_bits.origin,
            }
        ),
        "resolutions": [
            {
                "index": item.index,
                "size_x": item.size_x,
                "size_y": item.size_y,
                "size_z": item.size_z,
                "size_c": item.size_c,
                "size_t": item.size_t,
                "rgb_samples": item.rgb_samples,
                "dtype": item.dtype.str,
                "dimension_order": item.dimension_order,
                "is_thumbnail": item.is_thumbnail,
            }
            for item in value.resolutions
        ],
    }


def _series_from_mapping(value: object) -> SeriesInfo:
    """

    Reconstructs a native-series description from its saved JSON fields.

    Check the complete series and per-level field sets before constructing
    ResolutionInfo and SourceStorageBits objects. This prevents a missing level
    property from being replaced by an unintended constructor default.

    Args:
        value: Decoded dictionary for one saved series.

    Returns:
        SeriesInfo with ordered typed resolution records and optional stored-bit evidence.

    Raises:
        ValueError: Series, level or storage fields differ from the supported format.
    """

    if not isinstance(value, dict) or set(value) != {
        "index",
        "name",
        "resolutions",
        "storage_bits",
    }:
        raise ValueError("inspected source series fields differ")
    raw_storage = value["storage_bits"]
    if raw_storage is not None and (
        not isinstance(raw_storage, dict) or set(raw_storage) != {"bits", "origin"}
    ):
        raise ValueError("inspected source storage evidence fields differ")
    resolutions = value["resolutions"]
    if not isinstance(resolutions, list):
        raise ValueError("inspected source resolution inventory is invalid")
    fields = {
        "index",
        "size_x",
        "size_y",
        "size_z",
        "size_c",
        "size_t",
        "rgb_samples",
        "dtype",
        "dimension_order",
        "is_thumbnail",
    }
    parsed: list[ResolutionInfo] = []
    for item in resolutions:
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError("inspected source resolution fields differ")
        parsed.append(ResolutionInfo(**item))
    return SeriesInfo(
        index=value["index"],
        name=value["name"],
        resolutions=tuple(parsed),
        storage_bits=None if raw_storage is None else SourceStorageBits(**raw_storage),
    )


# ==============================================================================
#                 Choosing the analytical image and its metadata                
# ==============================================================================


def resolve_canonical_image_selection(
    ome: OME,
    series: tuple[SeriesInfo, ...],
    *,
    image_index: int | None = None,
    series_index: int | None = None,
    require_planar_channels: bool = False,
    allow_pending_organization: bool = False,
) -> CanonicalImageSelection:
    """

    Selects one analytical image from the matching OME and Bio-Formats series descriptions.

    A candidate must describe an actual full-resolution image with one Z
    position and time point, consistent spatial dimensions and a supported
    channel/dtype layout. Associated thumbnail series are excluded. The generic
    selector can accept RGB analytical images; quantitative mIF callers require
    separate scalar channels through require_planar_channels.

    When exactly one candidate qualifies, it can be selected automatically.
    Otherwise the caller supplies both image_index and series_index. These
    indices must match in the current reader layout and identify an eligible
    pair. A display name or filename is insufficient to resolve multiple images.

    Args:
        ome: Source OME metadata containing the image list.
        series: Reader-series descriptions corresponding to that list.
        image_index: Optional zero-based OME image position.
        series_index: Optional matching native reader-series position.
        require_planar_channels: Whether the selected analytical image must
            contain one scalar sample per separately stored channel.

    Returns:
        CanonicalImageSelection identifying the exact source image and native series.

    Raises:
        CanonicalIngestionError: Source descriptions disagree, supplied indices
            are incomplete/invalid, or zero or multiple eligible pairs remain
            without a valid explicit selection.
    """

    if not isinstance(ome, OME):
        raise CanonicalIngestionError("canonical selection requires OME metadata")
    if not isinstance(series, tuple):
        series = tuple(series)
    if len(ome.images) != len(series):
        raise CanonicalIngestionError(
            "OME image and Bio-Formats series inventories must have equal length"
        )

    explicit = image_index is not None or series_index is not None
    if explicit and (image_index is None or series_index is None):
        raise CanonicalIngestionError(
            "explicit canonical selection requires both image_index and series_index"
        )
    candidates = tuple(
        (index, item)
        for index, item in enumerate(series)
        if _is_analytical_pair(
            ome,
            index,
            item,
            require_planar_channels=require_planar_channels,
            allow_pending_organization=allow_pending_organization,
        )
    )
    if explicit:
        assert image_index is not None and series_index is not None
        if (
            isinstance(image_index, bool)
            or isinstance(series_index, bool)
            or not isinstance(image_index, int)
            or not isinstance(series_index, int)
        ):
            raise CanonicalIngestionError("canonical selection indices must be integers")
        if image_index != series_index:
            raise CanonicalIngestionError(
                "current Bio-Formats canonical selection requires aligned image/series indices"
            )
        if not 0 <= image_index < len(ome.images):
            raise CanonicalIngestionError("canonical image/series selection is out of range")
        item = series[series_index]
        if not _is_analytical_pair(
            ome,
            image_index,
            item,
            require_planar_channels=require_planar_channels,
            allow_pending_organization=allow_pending_organization,
        ):
            raise CanonicalIngestionError(
                "explicit canonical selection is not a supported analytical series"
            )
        selected_index = image_index
        selected_series = item
    else:
        if not candidates:
            raise CanonicalIngestionError(
                "no supported non-thumbnail analytical image/series was found"
            )
        if len(candidates) != 1:
            identities = ", ".join(f"{index}:{item.name}" for index, item in candidates)
            raise CanonicalIngestionError(
                "multiple analytical image/series candidates require explicit selection: "
                f"{identities}"
            )
        selected_index, selected_series = candidates[0]

    image = ome.images[selected_index]
    return CanonicalImageSelection(
        source_image_index=selected_index,
        source_series_index=selected_series.index,
        image_id=image.id,
        pixels_id=image.pixels.id,
        series_name=selected_series.name,
    )


def selected_canonical_ome(ome: OME, selection: CanonicalImageSelection) -> OME:
    """

    Creates a one-image OME view for the selected analytical image.

    Preserve the selected Image, Pixels and Channel IDs while placing the image
    at metadata position zero. The annotation helper updates image-indexed
    annotation links. Old TIFF directory mappings are cleared because any later
    standardized writer must record its own actual plane locations.

    Args:
        ome: Original source metadata containing all images.
        selection: Exact image choice whose IDs are checked before copying.

    Returns:
        Copied OME model containing only the selected analytical image.

    Raises:
        CanonicalIngestionError: The selection no longer matches the source image.
    """

    _validate_selection_identity(ome, selection)
    result = select_image_ome_metadata(ome, selection.source_image_index)
    result.images[0].pixels.tiff_data_blocks = []
    return result


# ==============================================================================
#                     Explicit standardized image conversion                    
# ==============================================================================


def publish_canonical_image(
    source_path: str | Path, output_path: str | Path, canonical_ome: OME,
    selection: CanonicalImageSelection, *, backend: BioFormatsBackend | None = None,
    overwrite: bool = False, reuse_existing: bool = False,
    require_stage1_review: bool = False,
) -> CanonicalPublicationResult:
    """Publish the reviewed selected source as an immutable sharded image.

    Source inspection and identity are retained once. Pixel copying preserves
    every native level and scalar value in bounded all-channel shards.
    """
    selected_backend = backend if backend is not None else get_default_backend()
    with selected_backend.open(Path(source_path).expanduser().resolve()) as source:
        authority = InspectedSourceAuthority.create(source_identity=source.identity,
            canonical_ome=source.metadata, series=source.series, selection=selection,
            backend_versions=source.versions)
        return _publish_canonical_source(source, authority, output_path, canonical_ome,
            overwrite=overwrite, reuse_existing=reuse_existing,
            require_stage1_review=require_stage1_review)


def publish_canonical_image_from_inspection(
    authority: InspectedSourceAuthority, output_path: str | Path, reviewed_ome: OME,
    *, backend: BioFormatsBackend | None = None, overwrite: bool = False,
    reuse_existing: bool = False, require_stage1_review: bool = False,
) -> CanonicalPublicationResult:
    """Copy the inspected image, reusing metadata for verified single-file sources.

    Reopening still verifies source bytes, reader versions and live image geometry.
    Grouped sources retain full metadata interpretation before copying pixels.
    """
    from .reviewed_source import _open_inspected_source

    with _open_inspected_source(authority, authority.canonical_ome, backend=backend) as source:
        return _publish_canonical_source(source, authority, output_path, reviewed_ome,
            overwrite=overwrite, reuse_existing=reuse_existing,
            require_stage1_review=require_stage1_review)


def _publish_canonical_source(source, authority, output_path, reviewed_ome, *,
        overwrite, reuse_existing, require_stage1_review):
    from .writers.processed_image import copy_source_pyramid, finish_processed_image
    from ..preprocessing.unmixing.output_publication import (
        authority_pointer_path, load_unmixing_output_publication, publish_unmixing_output)
    destination = Path(output_path).expanduser().resolve()
    if not destination.name.lower().endswith(".ome.zarr"):
        raise CanonicalIngestionError("Canonical output must end in .ome.zarr")
    if destination == authority.source_identity.resolved_path:
        raise CanonicalIngestionError("Canonical output cannot overwrite its source")
    from .aligned_collection import validate_collection_output
    validate_collection_output(authority.source_identity.resolved_path, destination)
    if overwrite and reuse_existing:
        raise ValueError("overwrite and reuse_existing are mutually exclusive")
    selection = authority.selection
    validate_reviewed_ome(source.metadata, reviewed_ome, selection,
        require_stage1_review=require_stage1_review)
    descriptor = source.series[selection.source_series_index]
    provenance_path = destination.with_name(destination.name + ".ingestion.json")
    request = {"schema_version": "canonical-ingestion/v2",
        "source_path": str(authority.source_identity.resolved_path),
        "source_sha256": authority.source_identity.file_sha256,
        "source_inspection_authority_sha256": authority.content_sha256,
        "output_path": str(destination),
        "source_image_index": selection.source_image_index,
        "source_image_id": selection.image_id, "source_pixels_id": selection.pixels_id,
        "source_series_index": selection.source_series_index,
        "source_series_name": selection.series_name,
        "pyramid_levels": len(descriptor.resolutions),
        "ome_xml_sha256": hashlib.sha256(to_xml(reviewed_ome).encode()).hexdigest(),
        "writer": "NGFF 0.5/Zarr 3 all-channel sharded writer",
        "source_pyramid_policy": "preserve_all_native_levels",
        "inner_chunk": [reviewed_ome.images[0].pixels.size_c,256,256],
        "shard": [reviewed_ome.images[0].pixels.size_c,1024,1024]}
    request_sha = _json_sha256(request)
    if authority_pointer_path(destination).exists() and not overwrite:
        if not reuse_existing:
            raise FileExistsError(f"Canonical publication already exists: {destination}")
        published = load_unmixing_output_publication(image_path=destination,
            provenance_path=provenance_path)
        if published.manifest.request_sha256 != request_sha:
            raise CanonicalIngestionError("Canonical image belongs to different source or reviewed settings")
        status = "reused"
    else:
        # A stable private work directory resumes only the same source and settings.
        from ..preprocessing.unmixing.publication_lock import _AuthorityPublicationLock
        import shutil
        work = destination.with_name(f".{destination.name}.copy-{request_sha[:24]}")
        with _AuthorityPublicationLock(work.with_name(work.name + ".processing")):
            copy_source_pyramid(source, reviewed_ome, work,
                series=selection.source_series_index, request_sha256=request_sha)
            downsamples = [0.5*(descriptor.resolutions[0].size_x/r.size_x +
                descriptor.resolutions[0].size_y/r.size_y) for r in descriptor.resolutions]
            finish_processed_image(work, reviewed_ome, level_downsamples=downsamples)
            _validate_inspected_source_stat(authority.source_identity)
            published = publish_unmixing_output(request_sha256=request_sha,
                image_path=destination, provenance_path=provenance_path,
                image_writer=lambda path: os.replace(work,path),
                provenance_bytes=_canonical_json_bytes(request), overwrite=overwrite)
            if work.exists():
                shutil.rmtree(work)
        status = "reused" if published.reused else "published"
    image_digest = next(a.content_sha256 for a in published.manifest.artifacts if a.role == "image")
    return CanonicalPublicationResult(status, authority.source_identity.resolved_path,
        published.image_path, published.provenance_path, authority.source_identity.file_sha256,
        image_digest, selection, len(descriptor.resolutions))


def _validate_inspected_source_stat(identity: OpenedSourceIdentity) -> None:
    """

    Checks whether the source still has the filesystem facts recorded during inspection.

    Compare its resolved path, byte size, modification time, device and file ID.
    This check detects ordinary replacement or editing without rereading the
    full image. The recorded source checksum remains the checksum calculated
    during the original inspection.

    Args:
        identity: OpenedSourceIdentity retained for the selected source.

    Raises:
        CanonicalIngestionError: The source disappeared or any compared file fact changed.
    """

    try:
        path = identity.resolved_path.resolve(strict=True)
        record = path / "zarr.json" if path.is_dir() else path
        current = record.stat()
    except OSError as exc:
        raise CanonicalIngestionError(
            "inspected source is missing during canonical publication"
        ) from exc
    if path != identity.resolved_path or (
        current.st_size,
        current.st_mtime_ns,
        current.st_dev,
        current.st_ino,
    ) != (
        identity.size_bytes,
        identity.modified_time_ns,
        identity.device_id,
        identity.inode_id,
    ):
        raise CanonicalIngestionError("source image stat identity changed after inspection")


def _is_analytical_pair(
    ome: OME,
    image_index: int,
    series: SeriesInfo,
    *,
    require_planar_channels: bool,
    allow_pending_organization: bool = False,
) -> bool:
    """

    Tests whether one OME image and reader series support the requested analytical layout.

    Args:
        ome: Source OME image list.
        image_index: Position of the image corresponding to series.
        series: Reader description of that image and its native resolutions.
        require_planar_channels: Whether RGB/interleaved analytical input is excluded.

    Returns:
        True only when the pair passes the analytical-image checks.
    """

    reasons = _analytical_pair_reasons(
        ome,
        image_index,
        series,
        require_planar_channels=require_planar_channels,
        collect_all_reasons=False,
    )
    if reasons == ("eligible_analytical_series",):
        return True
    if allow_pending_organization and require_planar_channels:
        from .acquired_plane_view import validate_source_dimensions
        try:
            validate_source_dimensions(ome, image_index, series)
        except ValueError:
            return False
        return True
    return False


def _analytical_pair_reasons(
    ome: OME,
    image_index: int,
    series: SeriesInfo,
    *,
    require_planar_channels: bool,
    collect_all_reasons: bool = True,
) -> tuple[SourceSeriesEligibilityReason, ...]:
    """

    Explains why a source image-series pair can or cannot be selected for analysis.

    The level-zero image must be an analytical image with supported Z/time
    dimensions. Its dimensions, scalar or RGB channel organization and pixel
    dtype must agree with OME. The planar option additionally requires one
    scalar sample per fluorescence channel.

    Args:
        ome: Metadata containing the matching source image.
        image_index: Image position to compare with the series index.
        series: Reader's series and native resolution descriptions.
        require_planar_channels: Whether separate scalar channel planes are required.
        collect_all_reasons: Whether to retain all discoverable failures for a
            selection table or stop early when only eligibility is needed.

    Returns:
        Tuple of failure reasons, or ("eligible_analytical_series",) when all
        required conditions pass.
    """

    reasons: list[SourceSeriesEligibilityReason] = []
    if not series.resolutions:
        reasons.append("missing_resolution_inventory")
    if series.index != image_index:
        reasons.append("unaligned_image_series_index")
    if not series.resolutions or (reasons and not collect_all_reasons):
        return tuple(reasons)
    base = series.resolutions[0]
    image = ome.images[image_index]
    pixels = image.pixels
    samples_per_pixel = tuple(int(channel.samples_per_pixel or 1) for channel in pixels.channels)
    if base.is_thumbnail:
        reasons.append("associated_thumbnail")
    if require_planar_channels and base.rgb_samples != 1:
        reasons.append("non_planar_channels")
    if any(value != 1 for value in (base.size_z, base.size_t, pixels.size_z, pixels.size_t)):
        reasons.append("unsupported_z_or_time")
    if base.size_x != pixels.size_x or base.size_y != pixels.size_y:
        reasons.append("dimension_mismatch")
    if (
        base.size_c * base.rgb_samples != pixels.size_c
        or sum(samples_per_pixel) != pixels.size_c
        or any(value != base.rgb_samples for value in samples_per_pixel)
    ):
        reasons.append("channel_layout_mismatch")
    # Eligibility-only calls stop after a failed layout check. The source
    # selection table continues so it can show every relevant reason.
    if reasons and not collect_all_reasons:
        return tuple(reasons)
    try:
        pixel_type_matches = np.dtype(base.dtype) == _ome_dtype(pixels.type.value)
    except CanonicalIngestionError:
        if not collect_all_reasons:
            raise
        pixel_type_matches = False
    if not pixel_type_matches:
        reasons.append("pixel_type_mismatch")
    return tuple(reasons) or ("eligible_analytical_series",)


def _validate_selection_identity(ome: OME, selection: CanonicalImageSelection) -> None:
    """

    Checks that the selected image still occupies its recorded position with the same OME IDs.

    Args:
        ome: Source metadata against which the selection is checked.
        selection: Recorded image position, Image ID and Pixels ID.

    Raises:
        CanonicalIngestionError: The selection type, image position or OME IDs differ.
    """

    if not isinstance(selection, CanonicalImageSelection):
        raise CanonicalIngestionError("canonical selection has an invalid type")
    if not 0 <= selection.source_image_index < len(ome.images):
        raise CanonicalIngestionError("canonical selection image index is out of range")
    image = ome.images[selection.source_image_index]
    if image.id != selection.image_id or image.pixels.id != selection.pixels_id:
        raise CanonicalIngestionError("canonical selection OME identity is stale")


# ==============================================================================
#                Validating the permitted metadata review changes               
# ==============================================================================


def validate_reviewed_ome(
    source_ome: OME,
    reviewed: OME,
    selection: CanonicalImageSelection,
    *,
    require_stage1_review: bool = False,
) -> None:
    """

    Checks that channel and acquisition review changed only the permitted image metadata.

    The selected image's ID, dimensions, dtype and channel IDs must remain
    unchanged. Raw review records its output panel separately from acquired bands.
    Separated component images require the complete per-channel component
    declaration. An existing nonempty fluorophore cannot be replaced with a
    conflicting value.

    To compare the remaining metadata, copy the selected source and reviewed
    image, reset permitted channel display/component changes in those comparison
    copies, remove their marker/component review annotations, and replay the
    saved acquisition completion. Their remaining OME values must then agree.
    This makes the allowed changes explicit rather than accepting any edited XML.

    Args:
        source_ome: Original inspected OME model.
        reviewed: One-image OME model returned after metadata review.
        selection: Exact image/series chosen from source_ome.
        require_stage1_review: Whether all channels must have completed
            component and biomarker review before this operation.

    Raises:
        CanonicalIngestionError: Source identity or geometry changed, required
            review is incomplete, fluorophores conflict, or other metadata was
            edited outside the permitted review operations.
    """

    _validate_selection_identity(source_ome, selection)
    if not isinstance(reviewed, OME) or len(reviewed.images) != 1:
        raise CanonicalIngestionError("reviewed canonical metadata must contain one image")
    source_image = source_ome.images[selection.source_image_index]
    reviewed_image = reviewed.images[0]
    if reviewed_image.id != selection.image_id:
        raise CanonicalIngestionError("reviewed canonical image identity is stale")
    source_pixels = source_image.pixels
    reviewed_pixels = reviewed_image.pixels
    component_inventory = resolve_component_identity_inventory(reviewed, 0)
    acquisition = resolve_unmixing_input_metadata(reviewed, 0)
    from .metadata.spectral import confirmed_raw_band_metadata, confirmed_unmixed_copy_metadata, resolve_spectral_state, resolve_prepared_channel_order

    spectral = resolve_spectral_state(reviewed, 0)
    raw_bands = spectral.state == "raw" or (acquisition is not None and acquisition.spectral_state == "raw")
    if require_stage1_review and not raw_bands and (
        component_inventory is None
        or component_inventory.mode is not ComponentIdentityMode.CHANNEL_FLUOROPHORES
    ):
        raise CanonicalIngestionError(
            "stage-one reviewed canonical metadata requires a typed per-channel "
            "component assertion"
        )
    if require_stage1_review and raw_bands and (
        component_inventory is None
        or component_inventory.mode is not ComponentIdentityMode.PLANNED_INVENTORY
        or component_inventory.component_biomarkers is None
    ):
        raise CanonicalIngestionError("Raw stage-one review requires an output component panel and marker decisions")
    scalar_fields = (
        "id",
        "dimension_order",
        "type",
        "size_x",
        "size_y",
        "size_z",
        "size_c",
        "size_t",
    )
    for field in scalar_fields:
        if getattr(source_pixels, field) != getattr(reviewed_pixels, field):
            raise CanonicalIngestionError(f"reviewed canonical Pixels.{field} differs from source")
    if len(source_pixels.channels) != len(reviewed_pixels.channels):
        raise CanonicalIngestionError("reviewed canonical channel topology differs from source")
    requires_component_channels = (
        not raw_bands
        and component_inventory is not None
        and component_inventory.channel_fluor_promoted
    )
    if requires_component_channels and any(
        not isinstance(channel.fluor, str) or not channel.fluor.strip()
        for channel in reviewed_pixels.channels
    ):
        raise CanonicalIngestionError(
            "reviewed canonical metadata requires non-empty Channel.Fluor for " "every channel"
        )
    if requires_component_channels:
        for channel in reviewed_pixels.channels:
            assert isinstance(channel.fluor, str)
            if channel.fluor != channel.fluor.strip():
                raise CanonicalIngestionError(
                    f"reviewed Channel.Fluor for {channel.id} has leading or " "trailing whitespace"
                )
    if require_stage1_review and not raw_bands:
        reviewed_rows = resolve_biomarker_assignment_rows(reviewed)
        try:
            apply_biomarker_assignment_rows(reviewed, reviewed_rows)
        except MetadataCanonicalizationError as exc:
            raise CanonicalIngestionError(
                f"stage-one reviewed biomarker metadata is invalid: {exc}"
            ) from exc
    for channel_index, (source_channel, reviewed_channel) in enumerate(
        zip(
            source_pixels.channels,
            reviewed_pixels.channels,
            strict=True,
        )
    ):
        if source_channel.id != reviewed_channel.id:
            raise CanonicalIngestionError("reviewed canonical channel identity is stale")
        if raw_bands and (source_channel.name != reviewed_channel.name or source_channel.fluor != reviewed_channel.fluor):
            raise CanonicalIngestionError("Raw component review must preserve acquired-band names and fluorophores")
        if source_channel.fluor != reviewed_channel.fluor:
            if isinstance(source_channel.fluor, str) and source_channel.fluor.strip():
                raise CanonicalIngestionError(
                    "reviewed canonical metadata must not overwrite a non-empty " "Channel.Fluor"
                )
            if not raw_bands and (
                component_inventory is None
                or component_inventory.mode is not ComponentIdentityMode.CHANNEL_FLUOROPHORES
                or not component_inventory.channel_fluor_promoted
                or reviewed_channel.fluor != component_inventory.component_names[channel_index]
            ):
                raise CanonicalIngestionError(
                    "reviewed canonical metadata changed Channel.Fluor without "
                    "an explicit per-channel component assertion"
                )
    # Check the rest of the image after accounting for permitted channel
    # display names, marker/component decisions and recorded acquisition
    # completion. Work on copies so comparison cannot change either input.
    resolve_biomarker_assignment_rows(reviewed)
    normalized_source = selected_canonical_ome(source_ome, selection)
    normalized_reviewed = reviewed.model_copy(deep=True)
    from .metadata.cycles import replay_channel_cycle_review
    normalized_source = replay_channel_cycle_review(normalized_source, normalized_reviewed)
    for source_channel, reviewed_channel in zip(
        normalized_source.images[0].pixels.channels,
        normalized_reviewed.images[0].pixels.channels,
        strict=True,
    ):
        reviewed_channel.name = source_channel.name
        reviewed_channel.fluor = source_channel.fluor
    _remove_biomarker_annotations(normalized_source)
    _remove_biomarker_annotations(normalized_reviewed)
    from .metadata.calibration import replay_calibration

    if spectral.state == "raw" and resolve_spectral_state(normalized_source, 0).state != "raw":
        normalized_source = confirmed_raw_band_metadata(normalized_source)
    elif ((spectral.state == "unmixed" and resolve_spectral_state(normalized_source, 0).state != "unmixed")
          or (resolve_prepared_channel_order(reviewed, 0) is not None
              and resolve_prepared_channel_order(normalized_source, 0) is None)):
        # Replay the explicit component review before deriving its confirmed state.
        from .metadata.canonical import apply_component_identity_inventory
        if component_inventory is not None:
            normalized_source = apply_component_identity_inventory(
                normalized_source, 0, component_inventory.component_names,
                mode=component_inventory.mode, decision_source=component_inventory.decision_source,
                promote_channel_fluorophores=component_inventory.channel_fluor_promoted,
            )
        normalized_source = confirmed_unmixed_copy_metadata(normalized_source, user_declared=True)
        for source_channel, original_channel in zip(normalized_source.images[0].pixels.channels, source_pixels.channels):
            source_channel.fluor = original_channel.fluor
    _remove_component_identity_annotations(normalized_source)
    _remove_component_identity_annotations(normalized_reviewed)
    normalized_source = replay_calibration(normalized_source, normalized_reviewed)
    if normalized_reviewed.model_dump(mode="json") != normalized_source.model_dump(mode="json"):
        raise CanonicalIngestionError(
            "reviewed canonical metadata changed outside permitted "
            "channel-name and biomarker decisions"
        )


def _remove_biomarker_annotations(ome: OME) -> None:
    """

    Removes linked marker-review records from a comparison copy of OME metadata.

    This allows validate_reviewed_ome() to compare unchanged image facts after
    separately checking the permitted biomarker decisions. Only channel-linked
    annotations in the biomarker namespace and their links are removed.

    Args:
        ome: Temporary OME copy used for comparison. It is modified in place.
    """

    annotations = ome.structured_annotations
    if annotations is None:
        return
    channel_reference_ids = {
        reference.id
        for image in ome.images
        for channel in image.pixels.channels
        for reference in channel.annotation_refs
    }
    biomarker_ids = {
        annotation.id
        for annotation in annotations.map_annotations
        if annotation.namespace == CHANNEL_METADATA_NAMESPACE
        and annotation.id in channel_reference_ids
    }
    annotations.map_annotations[:] = [
        annotation
        for annotation in annotations.map_annotations
        if annotation.id not in biomarker_ids
    ]
    for image in ome.images:
        for channel in image.pixels.channels:
            channel.annotation_refs[:] = [
                reference
                for reference in channel.annotation_refs
                if reference.id not in biomarker_ids
            ]
    if all(not getattr(annotations, field) for field in annotations.__class__.model_fields):
        ome.structured_annotations = None


def _remove_component_identity_annotations(ome: OME) -> None:
    """

    Removes component-review records and their image links from an OME comparison copy.

    Args:
        ome: Temporary copied model after its explicit component decisions
            have already been checked. An empty annotation container is removed.
    """

    annotations = ome.structured_annotations
    if annotations is None:
        return
    component_ids = {
        annotation.id
        for annotation in annotations.map_annotations
        if annotation.namespace == COMPONENT_IDENTITY_NAMESPACE
    }
    annotations.map_annotations[:] = [
        annotation
        for annotation in annotations.map_annotations
        if annotation.id not in component_ids
    ]
    for image in ome.images:
        image.annotation_refs[:] = [
            reference for reference in image.annotation_refs if reference.id not in component_ids
        ]
    if all(not getattr(annotations, field) for field in annotations.__class__.model_fields):
        ome.structured_annotations = None


def _ome_dtype(value: str) -> np.dtype:
    """

    Converts OME's fixed-width pixel types to NumPy dtypes.

    In particular, OME "float" means float32 and "double" means float64.
    Spelling these widths explicitly prevents NumPy's generic "float" alias
    from changing the interpreted source storage to float64.

    Args:
        value: OME pixel-type name.

    Returns:
        Corresponding NumPy dtype.

    Raises:
        CanonicalIngestionError: The supplied name cannot be resolved to a supported dtype.
    """

    try:
        # OME names fixed-width pixel types; NumPy's unqualified 'float'
        # instead means the platform Python float (normally float64).
        return np.dtype({"float": "float32", "double": "float64"}.get(value, value))
    except TypeError as exc:
        raise CanonicalIngestionError(f"unsupported canonical pixel type: {value}") from exc


__all__ = [
    "SourceSeriesEligibilityReason",
    "SourceSeriesCandidate",
    "SourceSeriesInventory",
    "inspect_source_series_inventory",
    "bind_source_series_selection",
    "CanonicalImageSelection",
    "CanonicalIngestionError",
    "CanonicalPublicationResult",
    "publish_canonical_image",
    "publish_canonical_image_from_inspection",
    "resolve_canonical_image_selection",
    "selected_canonical_ome",
]
