"""

Organizes image, channel and imaging settings into the OME metadata used by the pipeline.

OME describes images, their pixel dimensions, channels and acquisition planes.
Additional reviewed information is stored in MapAnnotations, which are named
key/value records. AnnotationRef links each record to the image or channel it
describes. These links let a channel keep its acquisition and marker assignments
when its display name changes.

During image import, typed OME settings supply exposure, gain, binning and offsets.
Confirmed Vectra fields supply missing processing state and stored-unit offsets.
Other supported reader mappings complete exposure or binning for their associated image.
Missing values are retained for review. Biomarker
and component assignments are then applied explicitly, with conflicting records
reported before an updated metadata copy is returned.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
import ast
import json
import math
from numbers import Integral, Real
import re
from typing import Any, Literal

from ome_types import OME
from ome_types.model import (
    AnnotationRef,
    Channel,
    Image,
    MapAnnotation,
    StructuredAnnotations,
)

from ..backends import BackendVersions, MetadataCanonicalizationError, MetadataConflictError

CHANNEL_METADATA_NAMESPACE = "urn:mif-pipeline:channel-metadata:v1"
COMPONENT_IDENTITY_NAMESPACE = "urn:mif-pipeline:component-identity:v1"
INGESTION_PROVENANCE_NAMESPACE = "urn:mif-pipeline:ingestion-provenance:v1"
ORIGINAL_METADATA_NAMESPACE = "openmicroscopy.org/OriginalMetadata"
UNMIXING_INPUT_NAMESPACE = "urn:mif-pipeline:unmixing-input:v2"
_UNMIXING_INPUT_V1_NAMESPACE = "urn:mif-pipeline:unmixing-input:v1"

_PROVENANCE_ANNOTATION_ID = "Annotation:IngestionProvenance:0"
_CHANNEL_ANNOTATION_ID = re.compile(r"^Annotation:ChannelMetadata:(?P<image>\d+):(?P<channel>\d+)$")
_UNMIXING_INPUT_ANNOTATION_ID = re.compile(r"^Annotation:UnmixingInput:(?P<image>\d+)$")
_COMPONENT_IDENTITY_ANNOTATION_ID = re.compile(r"^Annotation:ComponentIdentity:(?P<image>\d+)$")
_OFFSET_PROFILE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")


# ==============================================================================
#                           Image and channel records
# ==============================================================================


@dataclass(frozen=True, slots=True)
class BiomarkerAssignment:
    """

    Describes one researcher's biomarker choice for a channel.

    Attributes:
        image_index: Zero-based position of the image in OME.
        channel_index: Channel position within that image.
        biomarker: Biological target such as "CD8", or None for an explicit no-marker choice.
    """

    image_index: int
    channel_index: int
    biomarker: str | None = None


class ComponentIdentityMode(str, Enum):
    """

    Defines whether a reviewed component list describes planned panel membership or
    describes every existing image channel.

    PLANNED_INVENTORY stores a spectral panel list as an image annotation. The
    existing CHANNEL_FLUOROPHORES mode also supports prepared-channel labels;
    channel_fluor_promoted distinguishes explicit dye assertions from those labels.
    """

    PLANNED_INVENTORY = "planned_inventory"
    CHANNEL_FLUOROPHORES = "channel_fluorophores"


@dataclass(frozen=True, slots=True)
class ComponentIdentityInventory:
    """

    Stores ordered output components alongside the source channel inventory.

    Attributes:
        image_index: Position of the reviewed image.
        channel_ids: Channel IDs in the reviewed order.
        component_names: Ordered spectral components or prepared-channel labels; planned panels can differ in length from channel_ids.
        mode: Planned panel list or per-channel inventory.
        decision_source: Description of how the component review was supplied.
        channel_fluor_promoted: Whether the review asserts per-channel fluorophore identities.
        component_biomarkers: Explicit output marker decisions, or None before marker review.
    """

    image_index: int
    channel_ids: tuple[str, ...]
    component_names: tuple[str, ...]
    mode: ComponentIdentityMode
    decision_source: str
    channel_fluor_promoted: bool
    component_biomarkers: tuple[str | None, ...] | None = None
    nuclear_component_index: int | None = None
    nuclear_stain_reviewed: bool = False


@dataclass(frozen=True, slots=True)
class ChannelMetadata:
    """

    Collects a channel's names and biomarker-review state for use by the analysis.

    The assignment state distinguishes an assigned marker, an intentional no-marker
    choice and an incomplete review. Reading biomarker=None alone would lose that distinction.

    Attributes:
        image_index: OME image position.
        channel_index: Position of the channel within the image.
        channel_id: Persistent OME channel identifier.
        name: Channel display name, when supplied.
        fluorophore: Fluorescence-source name from Channel.Fluor.
        biomarker: Explicitly assigned biological target, or None.
        biomarker_assignment: "assigned", "none" or "missing".
    """

    image_index: int
    channel_index: int
    channel_id: str
    name: str | None
    fluorophore: str | None
    biomarker: str | None
    biomarker_assignment: Literal["assigned", "none", "missing"]


@dataclass(frozen=True, slots=True)
class ChannelMetadataIndex:
    """

    Provides repeated channel lookups after metadata has been decoded once.

    Records are grouped by image and channel position: index[0, 2] retrieves the
    third channel of the first image. Rebuild this index after changing the OME metadata
    so lookups reflect the new channel information.

    Attributes:
        _images: ChannelMetadata records grouped into one tuple per image.
    """

    _images: tuple[tuple[ChannelMetadata, ...], ...]

    def __iter__(self) -> Iterator[ChannelMetadata]:
        """

        Visits the stored channel records in image order and then channel order.

        Yields:
            One ChannelMetadata record per channel.
        """

        for channels in self._images:
            yield from channels

    def __getitem__(self, coordinate: object) -> ChannelMetadata:
        """

        Retrieves a channel using its image and channel positions.

        Args:
            coordinate: The pair (image_index, channel_index), using nonnegative indices.

        Returns:
            The corresponding ChannelMetadata record.

        Raises:
            MetadataCanonicalizationError: The index pair is invalid or outside the image/channel list.
        """

        if not isinstance(coordinate, tuple) or len(coordinate) != 2:
            raise MetadataCanonicalizationError(f"invalid OME channel coordinate: {coordinate!r}")
        image_index, channel_index = coordinate
        channels = self.for_image(image_index)
        normalized_channel = _canonical_runtime_index(
            channel_index,
            "channel",
        )
        if normalized_channel < 0:
            raise MetadataCanonicalizationError(
                f"OME channel index out of range: image {image_index}, "
                f"channel {normalized_channel}"
            )
        try:
            return channels[normalized_channel]
        except IndexError as exc:
            raise MetadataCanonicalizationError(
                f"OME channel index out of range: image {image_index}, "
                f"channel {normalized_channel}"
            ) from exc

    def for_image(self, image_index: int) -> tuple[ChannelMetadata, ...]:
        """

        Retrieves all channel records for one image.

        Args:
            image_index: Zero-based OME image position.

        Returns:
            The image's ChannelMetadata records in channel order.

        Raises:
            MetadataCanonicalizationError: The image index is invalid or out of range.
        """

        normalized_image = _canonical_runtime_index(image_index, "image")
        if normalized_image < 0:
            raise MetadataCanonicalizationError(f"OME image index out of range: {normalized_image}")
        try:
            return self._images[normalized_image]
        except IndexError as exc:
            raise MetadataCanonicalizationError(
                f"OME image index out of range: {normalized_image}"
            ) from exc


@dataclass(frozen=True, slots=True)
class UnmixingInputMetadata:
    """

    Collects the imaging settings needed to correct intensities before spectral fitting.

    Every per-band tuple follows band_order. Exposure is in seconds, offsets are in
    stored-pixel intensity units, and binning records both its square side length
    and area. For example, 2-by-2 acquisition binning has side 2 and area 4.

    Stored array width, meaningful digital range and camera digitizer depth are kept
    separate because an image can use only part of its storage range. The normalization
    step resolves which recorded range applies to the measured values.

    Attributes:
        image_index: Position of the selected image.
        spectral_state: "raw", "unmixed" or "unknown".
        state_source: Source of the recorded processing-state information.
        band_order: Measurement-band names in acquisition order.
        exposure_seconds: Recorded exposure in seconds; absent unused values remain None.
        gain: Per-band detector gain.
        binning: Square acquisition-binning side length per band.
        binning_area: Number of acquisition pixels combined per bin.
        source_storage_dtype: Stored pixel type, including known calibrated floating values.
        source_storage_bits: Bit width of that stored representation.
        source_storage_max: Integer storage maximum, or None for floating representations.
        camera_bit_depth: Optional retained digitizer-depth provenance; empty when absent.
            It does not determine the intensity correction.
        autofluorescence_subtracted: Known AF-subtraction state, or None when unknown.
        offsets: Recorded per-band detector offsets; unchanged when metadata says
            subtraction already occurred. Missing historical offsets can remain None.
        offset_authority: Metadata source or explicit profile supplying the offsets.
        correction_mode: Full correction, explicitly selected exposure_only, or automatic
            as_stored fitting for metadata-known exposure-inclusive calibrated values.
            Missing unapplied acquisition values remain None.
        intensity_representation: Known stored-value interpretation, or unknown.
        offset_profile_identifier: Name of an explicit offset profile, when used.
        acquisition_compatibility_profile_id: Profile identifying compatible imaging settings.
        acquisition_compatibility_profile_version: Version of that profile.
        instrument_compatibility_id: Instrument identity within the profile.
        optical_path_compatibility_id: Optical-path identity within the profile.
        detector_mode_compatibility_id: Detector-mode identity within the profile.
        digital_range_profile_id: Explicit meaningful-range profile, when supplied.
        digital_range_meaningful_bits: Meaningful bit count declared by that profile.
        digital_range_maximum: Maximum stored intensity declared by the profile.
        response_calibration_id: Identifier of a recorded measurement-response calibration.
    """

    image_index: int
    spectral_state: Literal["raw", "unmixed", "unknown"]
    state_source: str
    band_order: tuple[str, ...]
    exposure_seconds: tuple[float | None, ...]
    gain: tuple[float | None, ...]
    binning: tuple[int | None, ...]
    binning_area: tuple[int | None, ...]
    source_storage_dtype: str
    source_storage_bits: int
    source_storage_max: float | None
    camera_bit_depth: tuple[int, ...]
    autofluorescence_subtracted: bool | None
    offsets: tuple[float | None, ...]
    offset_authority: OffsetAuthority
    correction_mode: str = "full"
    intensity_representation: str = "unknown"
    offset_profile_identifier: str | None = None
    acquisition_compatibility_profile_id: str | None = None
    acquisition_compatibility_profile_version: str | None = None
    instrument_compatibility_id: str | None = None
    optical_path_compatibility_id: str | None = None
    detector_mode_compatibility_id: str | None = None
    digital_range_profile_id: str | None = None
    digital_range_meaningful_bits: int | None = None
    digital_range_maximum: float | None = None
    response_calibration_id: str | None = None


class OffsetAuthority(str, Enum):
    """

    Records where detector-offset values came from.

    METADATA records finite offsets resolved from typed OME or attributed vendor
    fields in its retained annotations. DECLARED_ALREADY_CORRECTED requires zero
    additional offsets. EXPLICIT_VALIDATED_PROFILE uses a named reviewed profile.
    """

    METADATA = "metadata"
    DECLARED_ALREADY_CORRECTED = "declared_already_corrected"
    EXPLICIT_VALIDATED_PROFILE = "explicit_validated_profile"


# ==============================================================================
#                          Detector-offset declarations
# ==============================================================================


def applied_detector_offset(offset: float | None, representation: str) -> float | None:
    """Derive the offset still to subtract without changing image metadata.

    ``dark_corrected_counts`` records that subtraction already occurred, so its
    applied offset is zero even when the historical detector setting is absent.
    Other representations retain the supplied setting and its missing state.
    Callers validate recorded values before applying this numerical decision.
    """
    return 0.0 if representation == "dark_corrected_counts" else offset


def validate_offset_specification(
    offsets: tuple[float, ...],
    authority: OffsetAuthority,
    profile_identifier: str | None,
) -> None:
    """

    Checks that detector offsets agree with their stated source.

    Already-corrected pixels require zero offsets.
    A reviewed offset profile requires its identifier. Other finite offsets are permitted
    when their source describes them in stored-pixel units.

    Args:
        offsets: Per-band detector offsets.
        authority: Recorded source of those offsets.
        profile_identifier: Name required for an explicit validated profile.

    Raises:
        MetadataConflictError: Offsets are invalid or disagree with the source/profile declaration.
    """

    if not isinstance(authority, OffsetAuthority):
        raise MetadataConflictError("offset authority is not a supported value")
    if any(not isinstance(value, Real) or isinstance(value, bool) for value in offsets):
        raise MetadataConflictError("offset values must be numeric")
    if any(not math.isfinite(float(value)) for value in offsets):
        raise MetadataConflictError("offset values must be finite")
    if authority is OffsetAuthority.DECLARED_ALREADY_CORRECTED and any(
        float(value) != 0.0 for value in offsets
    ):
        raise MetadataConflictError("declared already-corrected input requires zero offsets")
    if authority is OffsetAuthority.EXPLICIT_VALIDATED_PROFILE:
        if (
            not isinstance(profile_identifier, str)
            or _OFFSET_PROFILE_IDENTIFIER.fullmatch(profile_identifier) is None
        ):
            raise MetadataConflictError(
                "explicit validated offset authority requires a profile identifier"
            )
    elif profile_identifier is not None:
        raise MetadataConflictError(
            "offset profile identifier is permitted only for explicit validated profiles"
        )


# ==============================================================================
#                        Building reviewed image metadata
# ==============================================================================


def canonicalize_ome(
    source: OME,
    *,
    assignments: Iterable[BiomarkerAssignment] = (),
    versions: BackendVersions | None = None,
    source_image_indices: Iterable[int] = (),
    vectra_source_image_index: int | None = None,
    source_acquisition_values: Mapping[int, Mapping[str, tuple[float | int, ...]]] | None = None,
) -> OME:
    """

    Builds the reviewed OME metadata used by subsequent image processing.

    Start from a deep copy, check existing annotation IDs and links, then collect
    imaging settings for explicitly selected source images. Standard OME
    settings and supported reader mappings provide acquisition values. Incomplete acquisition
    values remain available for explicit completion. Apply the supplied biomarker
    choices and optionally add the image-reader versions.

    Equivalent repeated assignments reuse the same annotation. Conflicting values stop
    the operation before an updated metadata object is returned.

    Args:
        source: OME metadata obtained from the image reader.
        assignments: Explicit per-channel biomarker choices to apply.
        versions: Optional image-reader and backend versions to record.
        source_image_indices: Analytical scalar-channel images established by the
            initial reader inspection. Omit when editing or reopening normalized OME.
        vectra_source_image_index: Source image zero, only after the live backend proves
            the concrete Vectra reader and analytical series association. Omit when
            reopening normalized OME or applying reviewed assignments.
        source_acquisition_values: Exposure or binning vectors associated with image
            positions by the live reader's supported vendor mapping. Omit when
            reopening normalized OME; its linked acquisition records retain the values.

    Returns:
        The updated OME metadata for the analysis.

    Raises:
        MetadataCanonicalizationError: Source values or channel positions cannot be interpreted.
        MetadataConflictError: Existing records or reviewed choices disagree.
    """

    # Apply the review to a deep copy so rejected changes leave the source metadata intact.
    canonical = source.model_copy(deep=True)
    _validate_component_identity_bindings(canonical)

    # Initial inspection establishes image association once. A single collection
    # combines typed settings with missing-only vendor additions for each image.
    selected_indices = set()
    for image_index in source_image_indices:
        if type(image_index) is not int or not 0 <= image_index < len(canonical.images):
            raise MetadataCanonicalizationError("Source imaging-settings image position is invalid")
        selected_indices.add(image_index)
    if vectra_source_image_index is not None:
        if type(vectra_source_image_index) is not int or vectra_source_image_index != 0:
            raise MetadataCanonicalizationError("Vectra imaging settings belong to source image zero")
        if not canonical.images:
            raise MetadataCanonicalizationError("Vectra analytical source image zero is missing")
        selected_indices.add(vectra_source_image_index)
    if source_acquisition_values:
        for image_index in source_acquisition_values:
            if type(image_index) is not int or not 0 <= image_index < len(canonical.images):
                raise MetadataCanonicalizationError("Source imaging-settings image position is invalid")
            selected_indices.add(image_index)
    if selected_indices:
        from .calibration import retain_incomplete_acquisition

        for image_index in sorted(selected_indices):
            retain_incomplete_acquisition(
                canonical, image_index,
                vectra_source=image_index == vectra_source_image_index,
                source_acquisition_values=(source_acquisition_values or {}).get(image_index),
            )
    assignment_items = tuple(assignments)
    _validate_assignment_indices(canonical, assignment_items)

    for image_index, channel_index in _assignment_locations(assignment_items):
        matching = tuple(
            item
            for item in assignment_items
            if item.image_index == image_index and item.channel_index == channel_index
        )
        _apply_biomarker_assignment(canonical, image_index, channel_index, matching)

    if versions is not None:
        _apply_provenance(canonical, versions)

    return canonical


def apply_component_identity_inventory(
    source: OME,
    image_index: int,
    component_names: Iterable[str],
    *,
    mode: ComponentIdentityMode,
    decision_source: str = "user_review_csv_ui",
    component_biomarkers: tuple[str | None, ...] | None = None,
    nuclear_component_index: int | None = None,
    nuclear_stain_reviewed: bool = False,
    promote_channel_fluorophores: bool | None = None,
) -> OME:
    """

    Records reviewed spectral components or prepared-channel labels for one image.

    Require one component per channel only for per-channel fluorophore declarations.
    Separated channels retain distinct Channel IDs when their fluorophore labels repeat.
    Planned raw panels require unique components and have an independent output-component count. For a per-channel declaration,
    fill missing Channel.Fluor values and check any existing values against the same
    list. Prepared-channel label reviews set promote_channel_fluorophores=False:
    their labels do not identify dyes, and existing Channel.Fluor values stay unchanged.
    A planned-panel declaration is stored as an image-level record. The component
    review also records where the choice came from.

    Args:
        source: OME metadata to update through a copy.
        image_index: Position of the reviewed image.
        component_names: Ordered fluorescence-component names.
        mode: Planned-panel or per-channel inventory interpretation.
        decision_source: Description of the CSV or UI review supplying the names.
        component_biomarkers: Optional complete C-axis marker decisions; None means unreviewed.
        promote_channel_fluorophores: Whether names explicitly declare dyes. None
            retains the explicit per-channel declaration API's default; ordinary
            channel-label review supplies False.

    Returns:
        OME metadata containing the component declaration.

    Raises:
        MetadataCanonicalizationError: The mode, image or component coverage is invalid.
        MetadataConflictError: A reviewed name differs from an existing fluorophore or record.
    """

    canonical = canonicalize_ome(source)
    normalized_image = _canonical_runtime_index(image_index, "image")
    if normalized_image < 0 or normalized_image >= len(canonical.images):
        raise MetadataCanonicalizationError(f"OME image index out of range: {normalized_image}")
    if not isinstance(mode, ComponentIdentityMode):
        raise MetadataCanonicalizationError("component identity mode is unsupported")
    promoted = (mode is ComponentIdentityMode.CHANNEL_FLUOROPHORES
                if promote_channel_fluorophores is None else promote_channel_fluorophores)
    if type(promoted) is not bool or (promoted and mode is not ComponentIdentityMode.CHANNEL_FLUOROPHORES):
        raise MetadataCanonicalizationError("fluorophore promotion requires a per-channel declaration")
    if not isinstance(decision_source, str) or not decision_source.strip():
        raise MetadataCanonicalizationError(
            "component identity decision source must be non-empty text"
        )
    names = tuple(_component_identity(value) for value in component_names)
    channels = canonical.images[normalized_image].pixels.channels
    if not names:
        raise MetadataCanonicalizationError("component identities must be nonempty")
    if mode is ComponentIdentityMode.PLANNED_INVENTORY and len(set(names)) != len(names):
        raise MetadataCanonicalizationError("planned component identities must be unique")
    if mode is ComponentIdentityMode.CHANNEL_FLUOROPHORES and len(names) != len(channels):
        raise MetadataCanonicalizationError(
            f"component inventory has {len(names)} values for {len(channels)} channels"
        )
    if promoted:
        for channel, component_name in zip(channels, names, strict=True):
            if isinstance(channel.fluor, str) and channel.fluor != channel.fluor.strip():
                raise MetadataConflictError(
                    f"Channel.Fluor for {channel.id} has leading or trailing whitespace"
                )
            current = channel.fluor if _is_nonempty(channel.fluor) else None
            if current is not None and current != component_name:
                raise MetadataConflictError(
                    f"component {component_name!r} conflicts with existing "
                    f"Channel.Fluor {current!r} for {channel.id}"
                )
            if current is None:
                channel.fluor = component_name

    _validate_component_biomarkers(names, component_biomarkers)
    if type(nuclear_stain_reviewed) is not bool:
        raise MetadataCanonicalizationError("Nuclear-stain review state must be boolean")
    if nuclear_stain_reviewed:
        _validate_nuclear_component(nuclear_component_index, names, component_biomarkers)
    elif nuclear_component_index is not None:
        raise MetadataCanonicalizationError("A nuclear stain requires a reviewed choice")
    channel_ids = tuple(channel.id for channel in channels)
    value = {
        "schema_version": "1",
        "channel_ids": json.dumps(channel_ids, separators=(",", ":")),
        "component_names": json.dumps(names, separators=(",", ":")),
        "mode": mode.value,
        "decision_source": decision_source.strip(),
        "channel_fluor_promoted": str(promoted).lower(),
    }
    if component_biomarkers is not None:
        value["component_biomarkers"] = json.dumps(component_biomarkers, separators=(",", ":"))
    if nuclear_stain_reviewed:
        value["nuclear_component_index"] = json.dumps(nuclear_component_index)
    annotation_id = f"Annotation:ComponentIdentity:{normalized_image}"
    annotation = _ensure_annotation(
        canonical,
        annotation_id=annotation_id,
        namespace=COMPONENT_IDENTITY_NAMESPACE,
        value=value,
    )
    _link_once(canonical.images[normalized_image].annotation_refs, annotation.id)
    return canonical


def _validate_nuclear_component(index, names, biomarkers):
    if index is None:
        return
    if type(index) is not int or not 0 <= index < len(names):
        raise MetadataCanonicalizationError("Select a nuclear stain from the reviewed output components")
    if is_autofluorescence_component(names[index]):
        raise MetadataCanonicalizationError("Autofluorescence cannot be the nuclear stain")
    if biomarkers is not None and biomarkers[index] is not None:
        raise MetadataCanonicalizationError("Set the nuclear stain's marker assignment to No marker in Channels and markers")


def apply_nuclear_component_choice(source: OME, image_index: int, index: int | None) -> OME:
    """Return an OME copy identifying the reviewed DNA-stain component, or explicit None.

    The role supplements channel names, fluorophores and marker assignments;
    it does not add a channel or change image pixels.
    """
    inventory = resolve_component_identity_inventory(source, image_index)
    if inventory is None:
        raise MetadataCanonicalizationError("Review the output components before choosing a nuclear stain")
    _validate_nuclear_component(index, inventory.component_names, inventory.component_biomarkers)
    if index is not None and inventory.mode is ComponentIdentityMode.CHANNEL_FLUOROPHORES:
        if resolve_channel_metadata(source, image_index)[index].biomarker is not None:
            raise MetadataCanonicalizationError("Set the nuclear stain's marker assignment to No marker in Channels and markers")
    canonical = source.model_copy(deep=True)
    annotation_id = f"Annotation:ComponentIdentity:{image_index}"
    for position, annotation in enumerate(canonical.structured_annotations.map_annotations):
        if annotation.id == annotation_id:
            value = _annotation_value(annotation)
            value["nuclear_component_index"] = json.dumps(index)
            canonical.structured_annotations.map_annotations[position] = MapAnnotation(
                id=annotation.id, namespace=annotation.namespace, value=value)
            break
    return canonicalize_ome(canonical)


def remap_nuclear_component(inventory: ComponentIdentityInventory, component_names) -> int | None:
    """Return the DNA-stain index in a new component order, or None.

    Matching the saved component name keeps the role attached to the same stain
    when a fitted spectral library changes its output-channel position.
    """
    index = inventory.nuclear_component_index
    if index is None:
        return None
    identity = inventory.component_names[index]
    matches = [position for position, name in enumerate(component_names) if name == identity]
    if len(matches) != 1:
        raise MetadataCanonicalizationError(
            f"The nuclear stain {identity!r} must identify one output component; review Channels and markers")
    return matches[0]


def resolve_component_identity_inventory(
    ome: OME,
    image_index: int,
) -> ComponentIdentityInventory | None:
    """

    Reads the reviewed component list and checks it against the image's current channels.

    Saved channel IDs must match in order. When the review asserted per-channel
    fluorophores, their current Channel.Fluor values must also match the component list.

    Args:
        ome: Image metadata containing any component review.
        image_index: Position of the image to inspect.

    Returns:
        The checked ComponentIdentityInventory, or None when no review is linked.

    Raises:
        MetadataConflictError: The saved channel order or fluorophore declaration has changed.
    """

    normalized_image = _canonical_runtime_index(image_index, "image")
    if normalized_image < 0 or normalized_image >= len(ome.images):
        raise MetadataCanonicalizationError(f"OME image index out of range: {normalized_image}")
    annotation_id = f"Annotation:ComponentIdentity:{normalized_image}"
    annotations = _map_annotation_index(ome)
    linked_ids = {reference.id for reference in ome.images[normalized_image].annotation_refs}
    annotation = annotations.get(annotation_id)
    if annotation is None or annotation_id not in linked_ids:
        return None
    inventory = _decode_component_identity_annotation(annotation, normalized_image)
    actual_channel_ids = tuple(
        channel.id for channel in ome.images[normalized_image].pixels.channels
    )
    if inventory.channel_ids != actual_channel_ids:
        raise MetadataConflictError(
            "component identity inventory channel ID order differs from its OME image"
        )
    if inventory.channel_fluor_promoted:
        actual_fluors_list: list[str | None] = []
        for channel in ome.images[normalized_image].pixels.channels:
            if isinstance(channel.fluor, str) and channel.fluor != channel.fluor.strip():
                raise MetadataConflictError(
                    f"Channel.Fluor for {channel.id} has leading or trailing whitespace"
                )
            actual_fluors_list.append(channel.fluor if _is_nonempty(channel.fluor) else None)
        actual_fluors = tuple(actual_fluors_list)
        if actual_fluors != inventory.component_names:
            raise MetadataConflictError(
                "promoted component identity inventory differs from Channel.Fluor"
            )
    return inventory


# ==============================================================================
#              Selecting an image and updating its annotation links
# ==============================================================================


def select_image_ome_metadata(source: OME, source_image_index: int) -> OME:
    """

    Keeps one selected OME image and updates the annotation links that refer to its position.

    The selected image becomes position 0. Its Image, Pixels and Channel IDs are retained,
    while pipeline annotation IDs containing the former image position are renamed.
    Annotations referenced only by discarded images are removed; shared records remain
    when the retained image still uses them. Check for naming collisions and dangling
    references before returning the reduced metadata tree.

    Args:
        source: OME metadata containing the image and any associated images.
        source_image_index: Position of the analytical image selected by the reader.

    Returns:
        OME metadata describing only the selected image and its retained annotations.

    Raises:
        MetadataConflictError: Renaming would overwrite another annotation or leave an invalid link.
        IndexError: The selected image position is unavailable.
    """

    result = source.model_copy(deep=True)
    annotations_by_id = _annotation_index(result)
    _validate_managed_annotations(annotations_by_id.values())
    _validate_reserved_annotation_occupants(result, annotations_by_id)
    _validate_managed_annotation_ownership(result, annotations_by_id)
    _validate_component_identity_bindings(result)

    selected_image = result.images[source_image_index]
    removed_reference_ids: set[str] = set()
    for image_index, image in enumerate(result.images):
        if image_index != source_image_index:
            removed_reference_ids.update(_annotation_reference_ids(image))
    result.images = [selected_image]
    retained_reference_ids = _annotation_reference_ids(result)

    # Keep a shared annotation whenever an object remaining in the selected image still
    # references it.
    prune_ids = removed_reference_ids - retained_reference_ids
    rebase_ids: dict[str, str] = {}
    for annotation_id, annotation in annotations_by_id.items():
        namespace = getattr(annotation, "namespace", None)
        channel_match = _CHANNEL_ANNOTATION_ID.fullmatch(annotation_id)
        if namespace == CHANNEL_METADATA_NAMESPACE and channel_match is not None:
            image_index = int(channel_match["image"])
            if image_index != source_image_index:
                prune_ids.add(annotation_id)
                continue
            channel_index = int(channel_match["channel"])
            rebase_ids[annotation_id] = f"Annotation:ChannelMetadata:0:{channel_index}"
            continue
        unmixing_match = _UNMIXING_INPUT_ANNOTATION_ID.fullmatch(annotation_id)
        if namespace == UNMIXING_INPUT_NAMESPACE and unmixing_match is not None:
            image_index = int(unmixing_match["image"])
            if image_index != source_image_index:
                prune_ids.add(annotation_id)
                continue
            rebase_ids[annotation_id] = "Annotation:UnmixingInput:0"
            continue
        component_match = _COMPONENT_IDENTITY_ANNOTATION_ID.fullmatch(annotation_id)
        if namespace == COMPONENT_IDENTITY_NAMESPACE and component_match is not None:
            image_index = int(component_match["image"])
            if image_index != source_image_index:
                prune_ids.add(annotation_id)
                continue
            rebase_ids[annotation_id] = "Annotation:ComponentIdentity:0"

    # Check proposed annotation names for collisions before updating image links.
    retained_ids = set(annotations_by_id) - prune_ids
    for source_id, target_id in rebase_ids.items():
        if source_id == target_id:
            continue
        occupant = annotations_by_id.get(target_id)
        if occupant is not None and target_id in retained_ids:
            raise MetadataConflictError(
                f"Selected managed annotation rebase collides at {target_id}"
            )

    annotations = result.structured_annotations
    if annotations is not None:
        for field_name in StructuredAnnotations.model_fields:
            values = getattr(annotations, field_name)
            values[:] = [item for item in values if item.id not in prune_ids]
        for annotation in _all_annotations(result):
            annotation.id = rebase_ids.get(annotation.id, annotation.id)

    _rebase_retained_annotation_references(result, rebase_ids, prune_ids)
    if annotations is not None and all(
        not getattr(annotations, field_name) for field_name in StructuredAnnotations.model_fields
    ):
        result.structured_annotations = None

    selected_annotations = _annotation_index(result)
    _validate_managed_annotations(selected_annotations.values())
    _validate_reserved_annotation_occupants(result, selected_annotations)
    _validate_managed_annotation_ownership(result, selected_annotations)
    _validate_component_identity_bindings(result)
    return result


def _annotation_reference_ids(value: Any) -> set[str]:
    """

    Collects annotation IDs referenced anywhere within an OME object and its children.

    Args:
        value: OME model or subtree to inspect.

    Returns:
        The set of referenced annotation IDs.
    """

    return {
        reference.id
        for _, _, references in _iter_annotation_reference_owners(value)
        for reference in references
    }


def _iter_annotation_reference_owners(
    value: Any,
) -> Iterator[tuple[Any, tuple[str | int, ...], list[AnnotationRef]]]:
    """

    Finds every model object that carries an annotation-reference list.

    Recursively walk model fields and supported collections, accumulating field names
    and list positions into a structural path. The caller can then identify which
    image or channel contains a link, such as images[0].pixels.channels[1].annotation_refs.
    Already visited objects are skipped to avoid repeating shared subtrees or cycling.

    Args:
        value: OME model, subtree or collection from which traversal begins.

    Yields:
        The referring object, its structural path and its annotation-reference list.
    """

    seen: set[int] = set()

    def visit(
        item: Any,
        path: tuple[str | int, ...],
    ) -> Iterator[tuple[Any, tuple[str | int, ...], list[AnnotationRef]]]:
        """

        Visits one model value and recursively follows its fields or collection items.

        Scalars end the traversal. Dictionary keys and sequence indices extend the current
        path; model fields extend it by their names. When an annotation_refs field is found,
        emit its containing object and list before continuing through other fields.

        Args:
            item: Current model value or collection.
            path: Field names and positions leading to this value.

        Yields:
            Objects with annotation references, their paths and the corresponding lists.
        """

        if item is None or isinstance(item, (str, bytes, int, float, bool)):
            return

        # Track visited Python objects to avoid repeating shared subtrees or cycling through
        # references.
        identity = id(item)
        if identity in seen:
            return
        seen.add(identity)
        if isinstance(item, Mapping):
            for key, child in item.items():
                yield from visit(child, (*path, str(key)))
            return
        if isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                yield from visit(child, (*path, index))
            return
        fields = getattr(item.__class__, "model_fields", None)
        if fields is None:
            return
        references = getattr(item, "annotation_refs", None)
        if isinstance(references, list):
            yield item, (*path, "annotation_refs"), references
        for field_name in fields:
            if field_name != "annotation_refs":
                yield from visit(getattr(item, field_name), (*path, field_name))

    yield from visit(value, ())


def _format_model_path(path: tuple[str | int, ...]) -> str:
    """

    Formats a structural path as readable field and index notation for an error.

    Args:
        path: Field names and positions, for example ("images", 0, "pixels").

    Returns:
        Text such as "images[0].pixels".
    """

    formatted = ""
    for component in path:
        if isinstance(component, int):
            formatted += f"[{component}]"
        else:
            formatted += f".{component}" if formatted else component
    return formatted


def _rebase_retained_annotation_references(
    value: Any,
    rebase_ids: Mapping[str, str],
    prune_ids: set[str],
) -> None:
    """

    Updates links after selecting one image and renaming its image-indexed annotations.

    Check each retained object's references against the IDs being removed. Any remaining
    use of a removed annotation is a conflict. Otherwise apply the ID substitutions and
    collapse links that now refer to the same record.

    Args:
        value: Copied OME model whose references are updated in place.
        rebase_ids: Old annotation IDs mapped to their replacement IDs.
        prune_ids: IDs removed during image selection.

    Raises:
        MetadataConflictError: A retained object still references a removed annotation.
    """

    for _, path, references in _iter_annotation_reference_owners(value):
        discarded = tuple(reference.id for reference in references if reference.id in prune_ids)
        if discarded:
            raise MetadataConflictError(
                f"Retained OME object at {_format_model_path(path)} references "
                "discarded annotations: " + ", ".join(discarded)
            )
        _rebase_annotation_references(references, rebase_ids)


def _rebase_annotation_references(
    references: list[AnnotationRef],
    rebase_ids: Mapping[str, str],
) -> None:
    """

    Renames annotation references and keeps the first link to each resulting ID.

    Args:
        references: Reference list to update in place.
        rebase_ids: Old-to-new ID substitutions.
    """

    normalized: list[AnnotationRef] = []
    seen: set[str] = set()
    for reference in references:
        annotation_id = rebase_ids.get(reference.id, reference.id)
        if annotation_id in seen:
            continue
        normalized.append(AnnotationRef(id=annotation_id))
        seen.add(annotation_id)
    references[:] = normalized


# ==============================================================================
#                    Reading channel and acquisition records
# ==============================================================================


def resolve_channel_metadata_index(ome: OME) -> ChannelMetadataIndex:
    """

    Builds reusable channel records from the image metadata and linked biomarker decisions.

    Check annotation contents and their image/channel links, then collect channels in
    image order. Display name, fluorophore and biomarker are read separately. A channel
    without a biomarker review receives the "missing" assignment state.

    Args:
        ome: Current OME metadata.

    Returns:
        A ChannelMetadataIndex for direct image/channel lookup.

    Raises:
        MetadataConflictError: Channel annotations or their links are ambiguous or inconsistent.
    """

    annotations_by_id = _map_annotation_index(ome)
    images = []
    for image_index, image in enumerate(ome.images):
        channels = []
        for channel_index, channel in enumerate(image.pixels.channels):
            assignment, biomarker = _linked_biomarker_assignment(
                channel.annotation_refs,
                annotations_by_id,
                image_index,
                channel_index,
            )
            channels.append(
                ChannelMetadata(
                    image_index=image_index,
                    channel_index=channel_index,
                    channel_id=channel.id,
                    name=channel.name,
                    fluorophore=channel.fluor,
                    biomarker=biomarker,
                    biomarker_assignment=assignment,
                )
            )
        images.append(tuple(channels))
    return ChannelMetadataIndex(tuple(images))


def resolve_channel_metadata(ome: OME, image_index: int) -> tuple[ChannelMetadata, ...]:
    """

    Reads the channel records for one image using the shared channel-metadata index.

    Args:
        ome: Current OME metadata.
        image_index: Position of the image to inspect.

    Returns:
        Its ChannelMetadata records in channel order.
    """

    return resolve_channel_metadata_index(ome).for_image(image_index)


def _canonical_runtime_index(value: object, label: str) -> int:
    """

    Converts an integer-like image or channel index to a Python integer.

    Args:
        value: Candidate index; booleans are rejected.
        label: Index name used in errors.

    Returns:
        The integer position, which the caller checks against the available records.

    Raises:
        MetadataCanonicalizationError: The value is not an integer-like index.
    """

    if not isinstance(value, Integral) or isinstance(value, bool):
        raise MetadataCanonicalizationError(f"invalid OME {label} index: {value!r}")
    return int(value)


def resolve_unmixing_input_metadata(
    ome: OME,
    image_index: int,
) -> UnmixingInputMetadata | None:
    """

    Reads the saved imaging settings for an image and checks their channel order.

    The image must link to the expected acquisition annotation. After decoding its
    values, compare the saved Channel IDs with the image's current IDs. This keeps
    exposure, gain and offset vectors associated with the measurements they describe.

    Args:
        ome: Metadata containing any completed acquisition annotation.
        image_index: Position of the image to inspect.

    Returns:
        UnmixingInputMetadata, or None when no completed record is linked.

    Raises:
        MetadataCanonicalizationError: A field cannot be parsed.
        MetadataConflictError: The record format or ordered channel identities differ.
    """

    normalized_image = _canonical_runtime_index(image_index, "image")
    if normalized_image < 0 or normalized_image >= len(ome.images):
        raise MetadataCanonicalizationError(f"OME image index out of range: {normalized_image}")
    annotations_by_id = _annotation_index(ome)
    if any(
        getattr(annotation, "namespace", None) == _UNMIXING_INPUT_V1_NAMESPACE
        for annotation in annotations_by_id.values()
    ):
        raise MetadataConflictError("v1 unmixing input metadata requires source-backed migration")
    annotation_id = f"Annotation:UnmixingInput:{normalized_image}"
    annotations = _map_annotation_index(ome)
    linked_ids = {reference.id for reference in ome.images[normalized_image].annotation_refs}
    annotation = annotations.get(annotation_id)
    if annotation is None or annotation_id not in linked_ids:
        return None
    if annotation.namespace != UNMIXING_INPUT_NAMESPACE:
        raise MetadataConflictError(f"Conflicting annotation at deterministic ID {annotation_id}")
    metadata = _decode_unmixing_input_annotation(annotation, normalized_image)
    _validate_unmixing_input_channel_binding(
        ome,
        normalized_image,
        annotation,
        metadata,
    )
    return metadata


def _literal_response_calibration_id(value: object) -> str:
    """

    Checks a response-calibration name for nonblank printable text without padding.

    Args:
        value: Explicit calibration identifier.

    Returns:
        The valid calibration name.

    Raises:
        MetadataConflictError: The name has an invalid type, whitespace or nonprintable text.
    """

    if type(value) is not str or not value or not value.isprintable() or value != value.strip():
        raise MetadataConflictError(
            "response calibration ID must be nonempty canonical literal text"
        )
    return value


def _encode_unmixing_input_metadata(
    metadata: UnmixingInputMetadata,
    *,
    channel_ids: tuple[str, ...],
) -> dict[str, str]:
    """

    Converts imaging settings into the text fields supported by OME MapAnnotations.

    Per-band sequences are written as JSON lists alongside the corresponding Channel
    IDs. Optional profile fields use the format's empty-string representation, while
    unknown AF-subtraction state is recorded explicitly as "unknown".

    Args:
        metadata: Completed imaging settings.
        channel_ids: Image Channel IDs in the same order as the per-band vectors.

    Returns:
        String-valued fields for the acquisition annotation.
    """

    compact = {"separators": (",", ":")}
    return {
        "spectral_state": metadata.spectral_state,
        "correction_mode": metadata.correction_mode,
        "intensity_representation": metadata.intensity_representation,
        "is_unmixed_component": str(metadata.spectral_state == "unmixed").lower(),
        "state_source": metadata.state_source,
        "channel_ids": json.dumps(channel_ids, **compact),
        "band_order": json.dumps(metadata.band_order, **compact),
        "exposure_seconds": json.dumps(metadata.exposure_seconds, **compact),
        "gain": json.dumps(metadata.gain, **compact),
        "offsets": json.dumps(metadata.offsets, **compact),
        "offset_authority": metadata.offset_authority.value,
        "offset_profile_identifier": metadata.offset_profile_identifier or "",
        "binning": json.dumps(metadata.binning, **compact),
        "binning_area": json.dumps(metadata.binning_area, **compact),
        "source_storage_dtype": metadata.source_storage_dtype,
        "source_storage_bits": str(metadata.source_storage_bits),
        "source_storage_max": json.dumps(metadata.source_storage_max),
        "camera_bit_depth": json.dumps(metadata.camera_bit_depth, **compact),
        "response_calibration_id": (
            ""
            if metadata.response_calibration_id is None
            else _literal_response_calibration_id(metadata.response_calibration_id)
        ),
        "acquisition_compatibility_profile_id": (
            metadata.acquisition_compatibility_profile_id or ""
        ),
        "acquisition_compatibility_profile_version": (
            metadata.acquisition_compatibility_profile_version or ""
        ),
        "instrument_compatibility_id": metadata.instrument_compatibility_id or "",
        "optical_path_compatibility_id": (metadata.optical_path_compatibility_id or ""),
        "detector_mode_compatibility_id": (metadata.detector_mode_compatibility_id or ""),
        "digital_range_profile_id": metadata.digital_range_profile_id or "",
        "digital_range_meaningful_bits": (
            ""
            if metadata.digital_range_meaningful_bits is None
            else str(metadata.digital_range_meaningful_bits)
        ),
        "digital_range_maximum": (
            "" if metadata.digital_range_maximum is None else repr(metadata.digital_range_maximum)
        ),
        "autofluorescence_subtracted": (
            "unknown"
            if metadata.autofluorescence_subtracted is None
            else str(metadata.autofluorescence_subtracted).lower()
        ),
    }


def _decode_unmixing_input_annotation(
    annotation: MapAnnotation,
    image_index: int,
) -> UnmixingInputMetadata:
    """

    Reconstructs imaging settings from the saved annotation fields.

    Check the required field set, decode per-band JSON vectors and require consistent
    spectral-state declarations. Offset values are checked against their recorded source.
    An acquisition-compatibility profile needs all its instrument/path/mode fields;
    an explicit digital-range profile needs its ID, meaningful bits and maximum.

    Args:
        annotation: Saved acquisition MapAnnotation.
        image_index: Image position associated with the record.

    Returns:
        UnmixingInputMetadata containing the decoded acquisition values and profile IDs.

    Raises:
        MetadataConflictError: Fields or linked declarations are inconsistent.
        MetadataCanonicalizationError: Vector lengths or boolean declarations are invalid.
        TypeError, ValueError: A field cannot be converted to its required numerical type.
    """

    value = _annotation_value(annotation)
    required = {
        "spectral_state",
        "correction_mode",
        "intensity_representation",
        "is_unmixed_component",
        "state_source",
        "channel_ids",
        "band_order",
        "exposure_seconds",
        "gain",
        "offsets",
        "offset_authority",
        "offset_profile_identifier",
        "binning",
        "binning_area",
        "source_storage_dtype",
        "source_storage_bits",
        "source_storage_max",
        "camera_bit_depth",
        "autofluorescence_subtracted",
        "response_calibration_id",
        "acquisition_compatibility_profile_id",
        "acquisition_compatibility_profile_version",
        "instrument_compatibility_id",
        "optical_path_compatibility_id",
        "detector_mode_compatibility_id",
        "digital_range_profile_id",
        "digital_range_meaningful_bits",
        "digital_range_maximum",
    }
    if "channel_ids" not in value:
        raise MetadataConflictError(
            f"Managed unmixing input annotation requires channel_ids: {annotation.id}"
        )
    if set(value) != required:
        raise MetadataConflictError(
            f"Invalid unmixing input keys in managed annotation {annotation.id}"
        )
    response_calibration_literal = value["response_calibration_id"]
    response_calibration_id = (
        None
        if response_calibration_literal == ""
        else _literal_response_calibration_id(response_calibration_literal)
    )
    compatibility_profile = tuple(
        value.get(name, "")
        for name in (
            "acquisition_compatibility_profile_id",
            "acquisition_compatibility_profile_version",
            "instrument_compatibility_id",
            "optical_path_compatibility_id",
            "detector_mode_compatibility_id",
        )
    )
    if any(compatibility_profile) and not all(
        type(item) is str and item and item == item.strip() for item in compatibility_profile
    ):
        raise MetadataConflictError(
            "managed unmixing input compatibility profile must be complete literal text"
        )
    digital_range_profile = tuple(
        value.get(name, "")
        for name in (
            "digital_range_profile_id",
            "digital_range_meaningful_bits",
            "digital_range_maximum",
        )
    )
    if any(digital_range_profile) and not all(
        type(item) is str and item and item == item.strip() for item in digital_range_profile
    ):
        raise MetadataConflictError(
            "managed unmixing input digital-range profile must be complete literal text"
        )
    try:
        digital_range_meaningful_bits = (
            None if not digital_range_profile[1] else int(digital_range_profile[1])
        )
        digital_range_maximum = (
            None if not digital_range_profile[2] else float(digital_range_profile[2])
        )
    except ValueError as exc:
        raise MetadataConflictError(
            "managed unmixing input digital-range profile values are invalid"
        ) from exc
    if (digital_range_meaningful_bits is not None and digital_range_meaningful_bits <= 0) or (
        digital_range_maximum is not None
        and (not math.isfinite(digital_range_maximum) or digital_range_maximum <= 0.0)
    ):
        raise MetadataConflictError(
            "managed unmixing input digital-range profile values are invalid"
        )
    state = value["spectral_state"]
    if state not in {"raw", "unmixed", "unknown"}:
        raise MetadataConflictError(f"Invalid spectral_state in managed annotation {annotation.id}")
    is_unmixed = _parse_bool(value["is_unmixed_component"], "is_unmixed_component")
    if is_unmixed != (state == "unmixed"):
        raise MetadataConflictError(
            f"Conflicting spectral state in managed annotation {annotation.id}"
        )
    band_order = tuple(_json_list(value["band_order"], "band_order"))
    count = len(band_order)
    camera_bit_depth = _json_list(value["camera_bit_depth"], "camera_bit_depth")
    if camera_bit_depth:
        _require_vector_length(camera_bit_depth, "camera_bit_depth", count)
    try:
        offset_authority = OffsetAuthority(value["offset_authority"])
    except ValueError as exc:
        raise MetadataConflictError(
            f"Invalid offset authority in managed annotation {annotation.id}"
        ) from exc
    offset_profile_identifier = value["offset_profile_identifier"] or None
    mode = value["correction_mode"]
    if mode not in {"full", "exposure_only", "as_stored"}:
        raise MetadataConflictError("Unsupported intensity correction mode")
    representation = value["intensity_representation"]
    if representation not in {"unknown", "raw_counts", "dark_corrected_counts", "normalized_counts", "optical_density", "unsupported"}:
        raise MetadataConflictError("Unsupported stored intensity representation")
    if (mode == "as_stored") != (representation == "normalized_counts"):
        raise MetadataConflictError("Already-normalized intensity metadata requires as-stored correction")
    decoded_offsets = _json_vector(value["offsets"], "offsets", count)
    if any((item is None and mode not in {"exposure_only", "as_stored"} and representation != "dark_corrected_counts") or (item is not None and (not isinstance(item, Real) or isinstance(item, bool))) for item in decoded_offsets):
        raise MetadataConflictError("offset values must be numeric in managed annotation")
    offsets = tuple(None if item is None else float(item) for item in decoded_offsets)
    validate_offset_specification(
        tuple(item for item in offsets if item is not None),
        offset_authority,
        offset_profile_identifier,
    )
    def optional_numeric(name, integer=False):
        """Decode real recorded settings, retaining absent unused correction terms."""
        entries = _json_vector(value[name], name, count)
        result = []
        for item in entries:
            if item is None and (mode == "as_stored" or (mode == "exposure_only" and name != "exposure_seconds")):
                result.append(None)
            elif not isinstance(item, Real) or isinstance(item, bool) or not math.isfinite(item) or item <= 0 or (integer and int(item) != item):
                raise MetadataConflictError(f"Invalid imaging setting {name}")
            else:
                result.append(int(item) if integer else float(item))
        return tuple(result)

    return UnmixingInputMetadata(
        image_index=image_index,
        spectral_state=state,
        state_source=value["state_source"],
        band_order=tuple(str(item) for item in band_order),
        exposure_seconds=optional_numeric("exposure_seconds"),
        gain=optional_numeric("gain"),
        binning=optional_numeric("binning", integer=True),
        binning_area=optional_numeric("binning_area", integer=True),
        correction_mode=mode,
        intensity_representation=representation,
        source_storage_dtype=value["source_storage_dtype"],
        source_storage_bits=int(value["source_storage_bits"]),
        source_storage_max=json.loads(value["source_storage_max"]),
        camera_bit_depth=tuple(int(item) for item in camera_bit_depth),
        autofluorescence_subtracted=_parse_optional_bool(
            value["autofluorescence_subtracted"], "autofluorescence_subtracted"
        ),
        offsets=offsets,
        offset_authority=offset_authority,
        offset_profile_identifier=offset_profile_identifier,
        response_calibration_id=response_calibration_id,
        acquisition_compatibility_profile_id=(compatibility_profile[0] or None),
        acquisition_compatibility_profile_version=(compatibility_profile[1] or None),
        instrument_compatibility_id=(compatibility_profile[2] or None),
        optical_path_compatibility_id=(compatibility_profile[3] or None),
        detector_mode_compatibility_id=(compatibility_profile[4] or None),
        digital_range_profile_id=(digital_range_profile[0] or None),
        digital_range_meaningful_bits=digital_range_meaningful_bits,
        digital_range_maximum=digital_range_maximum,
    )


def _validate_unmixing_input_channel_binding(
    ome: OME,
    image_index: int,
    annotation: MapAnnotation,
    metadata: UnmixingInputMetadata,
) -> None:
    """

    Checks that imaging settings still refer to the same ordered image channels.

    The saved Channel IDs must match the current IDs exactly. Band names must be
    nonempty and cover the same count. This prevents an exposure or gain vector from
    being applied after channels have been reordered or replaced.

    Args:
        ome: Current image metadata.
        image_index: Position of the selected image.
        annotation: Acquisition annotation containing saved channel_ids.
        metadata: Decoded per-band settings.

    Raises:
        MetadataConflictError: Channel coverage, IDs or ordering differ.
    """

    value = _annotation_value(annotation)
    channels = ome.images[image_index].pixels.channels
    channel_ids = tuple(channel.id for channel in channels)
    if len(channel_ids) != len(metadata.band_order):
        raise MetadataConflictError(
            "managed unmixing input spectral axis conflicts with channel topology"
        )
    if any(not channel_id.strip() for channel_id in channel_ids) or len(channel_ids) != len(
        set(channel_ids)
    ):
        raise MetadataConflictError("managed unmixing input requires unique immutable channel IDs")
    if any(not band.strip() for band in metadata.band_order):
        raise MetadataConflictError(
            "managed unmixing input requires a complete stored spectral axis"
        )
    stored_channel_ids = tuple(
        str(item) for item in _json_list(value["channel_ids"], "channel_ids")
    )
    if stored_channel_ids != channel_ids:
        raise MetadataConflictError(
            "managed unmixing input channel ID order conflicts with OME channels"
        )


# ==============================================================================
#                      Parsing acquisition values and units
# ==============================================================================


def _literal_vector(value: str, name: str) -> tuple[Any, ...]:
    """

    Parses a vendor scalar or list written as literal text.

    Normalize true/false spellings, then use ast.literal_eval() to read values without
    executing expressions. Lists and tuples become tuples; a scalar becomes a one-item
    tuple. The field-specific parser subsequently checks units, types and band count.

    Args:
        value: Vendor text such as "[2, 4]", "[true, false]" or "2".
        name: Metadata field name included in errors.

    Returns:
        Parsed values as a tuple.

    Raises:
        MetadataCanonicalizationError: The text cannot be parsed as supported literals.
    """

    normalized = re.sub(r"\btrue\b", "True", value, flags=re.IGNORECASE)
    normalized = re.sub(r"\bfalse\b", "False", normalized, flags=re.IGNORECASE)
    try:
        decoded = ast.literal_eval(normalized)
    except (SyntaxError, ValueError) as exc:
        raise MetadataCanonicalizationError(
            f"invalid {name} OriginalMetadata value: {value!r}"
        ) from exc
    if isinstance(decoded, (list, tuple)):
        return tuple(decoded)
    return (decoded,)


def _boolean_vector(value: str, name: str, count: int) -> tuple[bool, ...]:
    """

    Reads one explicit boolean state for every measurement band.

    Args:
        value: Vendor scalar/vector text containing boolean declarations.
        name: Field name used in errors.
        count: Expected number of band values.

    Returns:
        Boolean values in band order.

    Raises:
        MetadataCanonicalizationError: A declaration or vector length is invalid.
    """

    result = tuple(_parse_bool(item, name) for item in _literal_vector(value, name))
    _require_vector_length(result, name, count)
    return result


def _parse_bool(value: object, name: str) -> bool:
    """

    Parses True/False and their supported text spellings as explicit boolean declarations.

    Args:
        value: True, False, "true", "false", "True" or "False".
        name: Field name used in errors.

    Returns:
        The declared boolean value.

    Raises:
        MetadataCanonicalizationError: The value is outside the supported declarations.
    """

    if value is True or value == "true" or value == "True":
        return True
    if value is False or value == "false" or value == "False":
        return False
    raise MetadataCanonicalizationError(f"{name} must be boolean, got {value!r}")


def _parse_optional_bool(value: object, name: str) -> bool | None:
    """

    Reads a boolean declaration while preserving the recorded "unknown" state.

    Args:
        value: A supported boolean value or the exact text "unknown".
        name: Field name used in errors.

    Returns:
        True or False for a known state, or None for "unknown".

    Raises:
        MetadataCanonicalizationError: The declaration is invalid.
    """

    if value == "unknown":
        return None
    return _parse_bool(value, name)


def _require_vector_length(value: tuple[Any, ...], name: str, count: int) -> None:
    """

    Checks that a per-band acquisition vector covers the expected number of measurements.

    Args:
        value: Parsed acquisition tuple.
        name: Quantity name used in errors.
        count: Required number of items.

    Raises:
        MetadataCanonicalizationError: The vector length differs from the band count.
    """

    if len(value) != count:
        raise MetadataCanonicalizationError(
            f"{name} has {len(value)} values for {count} measurement bands"
        )


def _json_list(value: str, name: str) -> list[Any]:
    """

    Parses a list stored as JSON text inside an annotation field.

    Args:
        value: JSON-encoded list.
        name: Annotation field name used in errors.

    Returns:
        The decoded list.

    Raises:
        MetadataConflictError: The text is invalid JSON or does not contain a list.
    """

    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise MetadataConflictError(f"Invalid JSON {name} in managed annotation") from exc
    if not isinstance(decoded, list):
        raise MetadataConflictError(f"{name} must be a JSON list in managed annotation")
    return decoded


def _json_vector(value: str, name: str, count: int) -> tuple[Any, ...]:
    """

    Reads a JSON list and checks that it supplies the expected number of band values.

    Args:
        value: JSON list text.
        name: Field name used in errors.
        count: Required item count.

    Returns:
        The decoded values as a tuple.

    Raises:
        MetadataConflictError: The JSON list is invalid.
        MetadataCanonicalizationError: The number of values differs from count.
    """

    result = tuple(_json_list(value, name))
    _require_vector_length(result, name, count)
    return result


def _validate_assignment_indices(
    ome: OME,
    assignments: tuple[BiomarkerAssignment, ...],
) -> None:
    """

    Checks that each biomarker assignment points to an existing image and channel.

    Args:
        ome: Metadata defining the available images and channels.
        assignments: Biomarker choices to apply.

    Raises:
        MetadataCanonicalizationError: An assignment position is negative or out of range.
    """

    for assignment in assignments:
        image_index = assignment.image_index
        channel_index = assignment.channel_index
        if image_index < 0 or image_index >= len(ome.images):
            raise MetadataCanonicalizationError(f"OME image index out of range: {image_index}")
        channel_count = len(ome.images[image_index].pixels.channels)
        if channel_index < 0 or channel_index >= channel_count:
            raise MetadataCanonicalizationError(
                f"OME channel index out of range: image {image_index}, channel {channel_index}"
            )


def _assignment_locations(
    assignments: tuple[BiomarkerAssignment, ...],
) -> tuple[tuple[int, int], ...]:
    """

    Collects unique image/channel positions from the biomarker choices.

    Args:
        assignments: Supplied biomarker assignments.

    Returns:
        Position pairs in first-occurrence order, allowing decisions for the same
        channel to be grouped and checked together.
    """

    return tuple(dict.fromkeys((item.image_index, item.channel_index) for item in assignments))


# ==============================================================================
#               Adding explicit review and reader-version records
# ==============================================================================


def _apply_biomarker_assignment(
    ome: OME,
    image_index: int,
    channel_index: int,
    assignments: tuple[BiomarkerAssignment, ...],
) -> None:
    """

    Records one consistent biomarker decision for a channel.

    Group the supplied decisions into an assigned marker or explicit no-marker choice.
    Repeated choices must agree with each other and with any existing annotation.
    Create or reuse the channel's annotation, then link it once from that channel.

    Args:
        ome: Metadata copy receiving the decision.
        image_index: Position of the image.
        channel_index: Position of the channel.
        assignments: Review choices for this channel.

    Raises:
        MetadataConflictError: Supplied choices or an existing decision disagree.
    """

    values = tuple(dict.fromkeys(_assignment_value(item) for item in assignments))
    if len(values) > 1:
        raise MetadataConflictError(
            f"Conflicting biomarker assignments for image {image_index}, "
            f"channel {channel_index}: {values}"
        )
    if not values:
        return

    channel = ome.images[image_index].pixels.channels[channel_index]
    expected = values[0]
    expected_value = _assignment_map_value(*expected)
    annotation_id = f"Annotation:ChannelMetadata:{image_index}:{channel_index}"
    reserved = _reserved_map_annotation(
        _annotation_index(ome),
        annotation_id=annotation_id,
        namespace=CHANNEL_METADATA_NAMESPACE,
    )
    if reserved is not None and _annotation_assignment(reserved) != expected:
        raise MetadataConflictError(
            f"Conflicting annotation at reserved deterministic ID {annotation_id}"
        )
    annotations_by_id = _map_annotation_index(ome)
    linked = _linked_assignment_annotations(
        channel.annotation_refs,
        annotations_by_id,
        image_index,
        channel_index,
    )
    existing = linked[0][1:] if linked else None
    if existing is not None and existing != expected:
        raise MetadataConflictError(
            f"Conflicting biomarker assignments for image {image_index}, "
            f"channel {channel_index}: {existing!r} and {expected!r}"
        )

    if linked:
        annotation = linked[0][0]
    elif reserved is not None:
        annotation = reserved
    else:
        annotation = _ensure_annotation(
            ome,
            annotation_id=annotation_id,
            namespace=CHANNEL_METADATA_NAMESPACE,
            value=expected_value,
        )
    _link_once(channel.annotation_refs, annotation.id)


def _apply_provenance(ome: OME, versions: BackendVersions) -> None:
    """

    Records the reader versions used to obtain the image metadata.

    Existing version records must agree with the supplied backend versions. Equivalent
    records are consolidated into one annotation and each image links to that record.

    Args:
        ome: Metadata copy to update.
        versions: Backend, bffile and Bio-Formats version information.

    Raises:
        MetadataConflictError: An existing reader-version record differs.
    """

    value = {
        "backend": versions.backend,
        "bffile.version": versions.bffile_version,
        "bioformats.maven_coordinate": versions.bioformats_maven_coordinate,
        "bioformats.runtime_version": versions.bioformats_runtime_version,
    }
    reserved = _validate_reserved_annotation_value(
        ome,
        annotation_id=_PROVENANCE_ANNOTATION_ID,
        namespace=INGESTION_PROVENANCE_NAMESPACE,
        value=value,
    )
    candidates = _provenance_annotations(ome)
    for candidate in candidates:
        if _annotation_value(candidate) != value:
            raise MetadataConflictError(f"Conflicting ingestion provenance at {candidate.id}")

    if candidates:
        annotation = reserved or min(candidates, key=lambda candidate: candidate.id)
    else:
        annotation = _ensure_annotation(
            ome,
            annotation_id=_PROVENANCE_ANNOTATION_ID,
            namespace=INGESTION_PROVENANCE_NAMESPACE,
            value=value,
        )
        candidates = (annotation,)

    provenance_ids = {candidate.id for candidate in candidates}
    if len(candidates) > 1:
        ome.structured_annotations.map_annotations[:] = [
            candidate
            for candidate in ome.structured_annotations.map_annotations
            if candidate.namespace != INGESTION_PROVENANCE_NAMESPACE
            or candidate.id == annotation.id
        ]
    for image in ome.images:
        _link_managed_once(image.annotation_refs, annotation.id, provenance_ids)


# ==============================================================================
#                   Checking and linking named OME annotations
# ==============================================================================


def _ensure_annotation(
    ome: OME,
    *,
    annotation_id: str,
    namespace: str,
    value: dict[str, str],
) -> MapAnnotation:
    """

    Creates a named annotation or reuses one containing the same fields and namespace.

    An existing ID must describe the same kind of record and values. This keeps a
    repeated review from creating duplicate annotations while exposing conflicting
    choices before links are changed.

    Args:
        ome: Metadata copy whose annotation collection may be extended.
        annotation_id: Record ID to create or reuse.
        namespace: Name identifying the annotation's format and meaning.
        value: String-valued fields to store.

    Returns:
        The created or matching MapAnnotation.

    Raises:
        MetadataConflictError: The ID is duplicated or occupied by a different record.
    """

    existing = _annotation_index(ome).get(annotation_id)
    if existing is None:
        annotation = MapAnnotation(id=annotation_id, namespace=namespace, value=value)
        if ome.structured_annotations is None:
            ome.structured_annotations = StructuredAnnotations()
        ome.structured_annotations.map_annotations.append(annotation)
        return annotation
    if not isinstance(existing, MapAnnotation):
        raise MetadataConflictError(f"Deterministic annotation ID is occupied: {annotation_id}")
    annotation = existing
    if annotation.namespace != namespace or _annotation_value(annotation) != value:
        raise MetadataConflictError(f"Conflicting annotation at deterministic ID {annotation_id}")
    return annotation


def _validate_reserved_annotation_value(
    ome: OME,
    *,
    annotation_id: str,
    namespace: str,
    value: dict[str, str],
) -> MapAnnotation | None:
    """

    Checks an existing annotation at the ID reserved for a particular pipeline record.

    Args:
        ome: Metadata containing the annotations.
        annotation_id: Expected record ID.
        namespace: Expected record type/format name.
        value: Expected string-valued fields.

    Returns:
        The matching MapAnnotation, or None when that ID is unused.

    Raises:
        MetadataConflictError: The ID is duplicated or its record differs.
    """

    annotation = _reserved_map_annotation(
        _annotation_index(ome),
        annotation_id=annotation_id,
        namespace=namespace,
    )
    if annotation is not None and _annotation_value(annotation) != value:
        raise MetadataConflictError(
            f"Conflicting annotation at reserved deterministic ID {annotation_id}"
        )
    return annotation


def _validate_reserved_annotation_occupants(
    ome: OME,
    annotations_by_id: Mapping[str, Any],
) -> None:
    """

    Checks that image/channel-indexed annotation names describe valid locations.

    For example, "Annotation:ChannelMetadata:0:2" must contain a channel-metadata
    record and refer to the third channel of the first image. Apply the corresponding
    rules to image-level component and acquisition records and the reader-version ID.

    Args:
        ome: Metadata defining the image/channel positions.
        annotations_by_id: Lookup of all structured annotations.

    Raises:
        MetadataConflictError: A reserved name has the wrong record type or an invalid position.
    """

    for annotation_id in annotations_by_id:
        match = _CHANNEL_ANNOTATION_ID.fullmatch(annotation_id)
        if match is not None:
            _reserved_map_annotation(
                annotations_by_id,
                annotation_id=annotation_id,
                namespace=CHANNEL_METADATA_NAMESPACE,
            )
            image_index = int(match["image"])
            channel_index = int(match["channel"])
            if image_index >= len(ome.images) or channel_index >= len(
                ome.images[image_index].pixels.channels
            ):
                raise MetadataConflictError(
                    f"Reserved deterministic annotation ID has no owning channel: "
                    f"{annotation_id}"
                )
        elif annotation_id == _PROVENANCE_ANNOTATION_ID:
            _reserved_map_annotation(
                annotations_by_id,
                annotation_id=annotation_id,
                namespace=INGESTION_PROVENANCE_NAMESPACE,
            )
        elif (match := _UNMIXING_INPUT_ANNOTATION_ID.fullmatch(annotation_id)) is not None:
            _reserved_map_annotation(
                annotations_by_id,
                annotation_id=annotation_id,
                namespace=UNMIXING_INPUT_NAMESPACE,
            )
            if int(match["image"]) >= len(ome.images):
                raise MetadataConflictError(
                    "Reserved deterministic annotation ID has no owning image: "  f"{annotation_id}"
                )
        elif (match := _COMPONENT_IDENTITY_ANNOTATION_ID.fullmatch(annotation_id)) is not None:
            _reserved_map_annotation(
                annotations_by_id,
                annotation_id=annotation_id,
                namespace=COMPONENT_IDENTITY_NAMESPACE,
            )
            if int(match["image"]) >= len(ome.images):
                raise MetadataConflictError(
                    "Reserved deterministic annotation ID has no owning image: "  f"{annotation_id}"
                )


def _reserved_map_annotation(
    annotations_by_id: Mapping[str, Any],
    *,
    annotation_id: str,
    namespace: str,
) -> MapAnnotation | None:
    """

    Looks up an annotation ID and checks its expected type and namespace.

    Args:
        annotations_by_id: Annotation lookup for the metadata tree.
        annotation_id: ID to inspect.
        namespace: Required record type/format name.

    Returns:
        The matching MapAnnotation, or None when the ID is absent.

    Raises:
        MetadataConflictError: The ID belongs to another annotation type or namespace.
    """

    annotation = annotations_by_id.get(annotation_id)
    if annotation is None:
        return None
    if not isinstance(annotation, MapAnnotation) or annotation.namespace != namespace:
        raise MetadataConflictError(
            f"Reserved deterministic annotation ID is occupied: {annotation_id}"
        )
    return annotation


def _provenance_annotations(ome: OME) -> tuple[MapAnnotation, ...]:
    """

    Collects the annotations recording image-reader versions.

    Args:
        ome: Metadata whose annotations should be inspected.

    Returns:
        MapAnnotations in the ingestion-provenance namespace.

    Raises:
        MetadataConflictError: IDs repeat or a version record uses an incompatible type.
    """

    candidates: list[MapAnnotation] = []
    for annotation in _annotation_index(ome).values():
        if getattr(annotation, "namespace", None) != INGESTION_PROVENANCE_NAMESPACE:
            continue
        if not isinstance(annotation, MapAnnotation):
            raise MetadataConflictError(
                f"Ingestion provenance must be a MapAnnotation: {annotation.id}"
            )
        candidates.append(annotation)
    return tuple(candidates)


def _annotation_index(ome: OME) -> dict[str, Any]:
    """

    Builds an ID lookup across all structured-annotation types and checks for duplicate IDs.

    Args:
        ome: Metadata containing the annotations.

    Returns:
        Annotation IDs mapped to their records.

    Raises:
        MetadataConflictError: An ID occurs more than once.
    """

    annotations_by_id: dict[str, Any] = {}
    for annotation in _all_annotations(ome):
        annotation_id = annotation.id
        if annotation_id in annotations_by_id:
            raise MetadataConflictError(f"Duplicate OME structured annotation ID: {annotation_id}")
        annotations_by_id[annotation_id] = annotation
    return annotations_by_id


def _map_annotation_index(ome: OME) -> dict[str, MapAnnotation]:
    """

    Checks pipeline annotations and builds the map-record lookup used by metadata readers.

    First check IDs across all annotation types. Then validate recognized record
    contents and verify that their references belong to the intended images/channels.
    This ensures that filtering for MapAnnotations cannot hide a conflicting record type.

    Args:
        ome: Metadata to inspect.

    Returns:
        IDs mapped to their MapAnnotation records.

    Raises:
        MetadataConflictError: Records or their image/channel links are inconsistent.
    """

    annotations = _annotation_index(ome)
    _validate_managed_annotations(annotations.values())
    _validate_reserved_annotation_occupants(ome, annotations)
    _validate_managed_annotation_ownership(ome, annotations)
    return {
        annotation_id: annotation
        for annotation_id, annotation in annotations.items()
        if isinstance(annotation, MapAnnotation)
    }


def _all_annotations(ome: OME) -> Iterator[Any]:
    """

    Visits the annotation collections under OME StructuredAnnotations.

    Args:
        ome: Metadata containing an optional structured-annotation section.

    Yields:
        Each stored annotation in its collection order.
    """

    if ome.structured_annotations is None:
        return
    for field_name in StructuredAnnotations.model_fields:
        yield from getattr(ome.structured_annotations, field_name)


def _link_once(references: list[AnnotationRef], annotation_id: str) -> None:
    """

    Ensures that an image or channel contains exactly one link to the chosen annotation.

    Keep the first existing link, remove repeats of that ID and append it if absent.

    Args:
        references: Annotation-reference list to update.
        annotation_id: ID that should be linked once.
    """

    # Keep one link to the chosen ID while preserving the order of other references.
    normalized: list[AnnotationRef] = []
    found = False
    for reference in references:
        if reference.id != annotation_id:
            normalized.append(reference)
        elif not found:
            normalized.append(reference)
            found = True
    if not found:
        normalized.append(AnnotationRef(id=annotation_id))
    references[:] = normalized


def _link_managed_once(
    references: list[AnnotationRef],
    annotation_id: str,
    managed_ids: set[str],
) -> None:
    """

    Replaces references to equivalent records with one link to their chosen representative.

    Args:
        references: Reference list to update.
        annotation_id: Representative ID to retain.
        managed_ids: IDs of the equivalent records being consolidated.
    """

    references[:] = [reference for reference in references if reference.id not in managed_ids]
    references.append(AnnotationRef(id=annotation_id))


def _linked_biomarker_assignment(
    references: list[AnnotationRef],
    annotations_by_id: Mapping[str, MapAnnotation],
    image_index: int,
    channel_index: int,
) -> tuple[Literal["assigned", "none", "missing"], str | None]:
    """

    Reads the channel's linked biomarker decision and preserves its review state.

    Args:
        references: Annotation references on the channel.
        annotations_by_id: Checked MapAnnotation lookup.
        image_index: Image position used in errors.
        channel_index: Channel position used in errors.

    Returns:
        ("assigned", marker), ("none", None) or ("missing", None).

    Raises:
        MetadataConflictError: The linked decisions are invalid, duplicated or conflicting.
    """

    linked = _linked_assignment_annotations(
        references,
        annotations_by_id,
        image_index,
        channel_index,
    )
    if not linked:
        return "missing", None
    return linked[0][1], linked[0][2]


def _linked_assignment_annotations(
    references: list[AnnotationRef],
    annotations_by_id: Mapping[str, MapAnnotation],
    image_index: int,
    channel_index: int,
) -> tuple[tuple[MapAnnotation, Literal["assigned", "none"], str | None], ...]:
    """

    Finds the channel's explicit marker record and rejects competing assignment records.

    Repeated references to one ID are inspected once. More than one distinct assignment
    record is rejected even if marker text agrees, keeping one review decision per channel.

    Args:
        references: References attached to the channel.
        annotations_by_id: Checked map-record lookup.
        image_index: Image position included in errors.
        channel_index: Channel position included in errors.

    Returns:
        An empty tuple or one tuple containing the annotation, state and marker.

    Raises:
        MetadataConflictError: Distinct assignment records repeat or disagree.
    """

    linked = tuple(
        (annotation, *_annotation_assignment(annotation))
        for reference in references
        if (annotation := annotations_by_id.get(reference.id)) is not None
        and annotation.namespace == CHANNEL_METADATA_NAMESPACE
    )
    unique: dict[
        str,
        tuple[MapAnnotation, Literal["assigned", "none"], str | None],
    ] = {}
    for item in linked:
        unique.setdefault(item[0].id, item)
    deduplicated = tuple(unique.values())
    values = tuple(dict.fromkeys((state, biomarker) for _, state, biomarker in deduplicated))
    if len(values) > 1:
        raise MetadataConflictError(
            f"Conflicting linked biomarker assignments for image {image_index}, "
            f"channel {channel_index}: {values}"
        )
    if len(deduplicated) > 1:
        raise MetadataConflictError(
            f"Duplicate linked biomarker assignments for image {image_index}, "
            f"channel {channel_index}: {tuple(item[0].id for item in deduplicated)}"
        )
    return deduplicated


def _annotation_value(annotation: MapAnnotation) -> dict[str, str]:
    """

    Reads a map annotation into a dictionary while detecting repeated field names.

    Args:
        annotation: MapAnnotation containing named text values.

    Returns:
        The annotation's key/value dictionary.

    Raises:
        MetadataConflictError: A named field appears more than once.
    """

    result: dict[str, str] = {}
    for entry in annotation.value.ms:
        if entry.k is None:
            continue
        if entry.k in result:
            raise MetadataConflictError(
                f"Duplicate map key {entry.k!r} in managed annotation {annotation.id}"
            )
        result[entry.k] = entry.value
    return result


def _annotation_assignment(
    annotation: MapAnnotation,
) -> tuple[Literal["assigned", "none"], str | None]:
    """

    Decodes an explicit biomarker-review state and its associated marker.

    "assigned" requires nonblank marker text. "none" records a deliberate no-marker
    choice and omits the biomarker field. A record without the state cannot establish
    that channel review was completed.

    Args:
        annotation: Channel biomarker MapAnnotation.

    Returns:
        The assignment state and marker text, or None for a no-marker choice.

    Raises:
        MetadataConflictError: Fields or the marker/state combination are inconsistent.
    """

    entries = [entry.value for entry in annotation.value.ms if entry.k == "biomarker"]
    if len(entries) > 1:
        raise MetadataConflictError(f"duplicate biomarker keys in MapAnnotation {annotation.id}")
    states = [entry.value for entry in annotation.value.ms if entry.k == "biomarker_assignment"]
    if len(states) > 1:
        raise MetadataConflictError(
            f"duplicate biomarker_assignment keys in MapAnnotation {annotation.id}"
        )

    value = _annotation_value(annotation)
    unexpected = set(value) - {"biomarker_assignment", "biomarker"}
    if unexpected:
        raise MetadataConflictError(
            f"Unexpected channel metadata keys in managed annotation "
            f"{annotation.id}: {sorted(unexpected)}"
        )
    biomarker = entries[0] if entries and _is_nonempty(entries[0]) else None
    if not states:
        raise MetadataConflictError(
            f"Missing biomarker_assignment in managed annotation {annotation.id}"
        )

    state = states[0]
    if state == "assigned" and biomarker is not None:
        return "assigned", biomarker

    # An explicit no-marker decision stores its review state and omits the biomarker field.
    if state == "none" and not entries:
        return "none", None
    raise MetadataConflictError(
        f"Invalid biomarker assignment in managed annotation {annotation.id}"
    )


def _component_identity(value: object) -> str:
    """

    Reads a required fluorescence-component name and trims surrounding whitespace.

    Args:
        value: Component name supplied during review.

    Returns:
        The cleaned name.

    Raises:
        MetadataCanonicalizationError: The value is blank, non-text or the reserved "None" token.
    """

    if not isinstance(value, str) or not value.strip():
        raise MetadataCanonicalizationError("component identities must be non-empty text")
    normalized = value.strip()
    if normalized == "None":
        raise MetadataCanonicalizationError(
            "component identity must not use the reserved None sentinel"
        )
    return normalized


def is_autofluorescence_component(name: str) -> bool:
    """Recognize the existing AF component aliases without interpreting band labels."""
    from .cycles import split_cycle_component
    name, _cycle = split_cycle_component(name)
    normalized = " ".join(name.casefold().replace("_", " ").replace("-", " ").split())
    return normalized in {"af", "sample af", "autofluorescence", "sample autofluorescence"}


def _validate_component_biomarkers(names, biomarkers):
    """Validate explicit C-axis marker decisions without assigning source bands."""
    if biomarkers is None:
        return
    if (not isinstance(biomarkers, tuple) or len(biomarkers) != len(names)
            or any(marker is not None and (not isinstance(marker, str)
                or not marker.strip() or marker != marker.strip()) for marker in biomarkers)):
        raise MetadataCanonicalizationError("component markers must cover every component with text or None")
    from .cycles import split_cycle_component
    assigned = [(marker, split_cycle_component(name)[1])
                for name, marker in zip(names, biomarkers, strict=True) if marker is not None]
    if len(set(assigned)) != len(assigned):
        raise MetadataCanonicalizationError("component markers must be unique")
    for name, marker in zip(names, biomarkers, strict=True):
        if is_autofluorescence_component(name) and marker is not None:
            raise MetadataCanonicalizationError("autofluorescence has no biological marker")


def _decode_component_identity_annotation(
    annotation: MapAnnotation,
    image_index: int,
) -> ComponentIdentityInventory:
    """

    Reads the component names and determines what the review asserted about the image channels.

    Require one component per channel ID only for per-channel declarations; their
    fluorophore labels may repeat. Planned raw panels require unique component labels.
    Only explicit per-channel dye declarations set channel_fluor_promoted; ordinary
    prepared-channel labels retain the same inventory without asserting dyes.

    Args:
        annotation: Component-review MapAnnotation.
        image_index: Position of the image described by the record.

    Returns:
        A ComponentIdentityInventory containing the decoded names and review mode.

    Raises:
        MetadataConflictError: Fields, channel coverage or mode declarations disagree.
    """

    value = _annotation_value(annotation)
    required = {
        "schema_version",
        "channel_ids",
        "component_names",
        "mode",
        "decision_source",
        "channel_fluor_promoted",
    }
    if set(value) - {"component_biomarkers", "nuclear_component_index"} != required or value["schema_version"] != "1":
        raise MetadataConflictError(
            f"Invalid component identity annotation schema in {annotation.id}"
        )
    channel_ids = tuple(_json_list(value["channel_ids"], "channel_ids"))
    component_names = tuple(
        _component_identity(item)
        for item in _json_list(value["component_names"], "component_names")
    )
    if (
        not channel_ids
        or any(not isinstance(item, str) or not item.strip() for item in channel_ids)
        or not component_names
    ):
        raise MetadataConflictError(
            "component identity annotation has invalid channel/component inventory"
        )
    try:
        mode = ComponentIdentityMode(value["mode"])
    except ValueError as exc:
        raise MetadataConflictError(
            f"Unsupported component identity mode in {annotation.id}"
        ) from exc
    if mode is ComponentIdentityMode.PLANNED_INVENTORY and len(set(component_names)) != len(component_names):
        raise MetadataConflictError("planned component identities must be unique")
    if mode is ComponentIdentityMode.CHANNEL_FLUOROPHORES and len(channel_ids) != len(component_names):
        raise MetadataConflictError("per-channel component inventory must cover every channel")
    biomarkers = (tuple(_json_list(value["component_biomarkers"], "component_biomarkers"))
                  if "component_biomarkers" in value else None)
    _validate_component_biomarkers(component_names, biomarkers)
    reviewed = "nuclear_component_index" in value
    try:
        nuclear = json.loads(value["nuclear_component_index"]) if reviewed else None
    except (TypeError, ValueError) as exc:
        raise MetadataConflictError("Nuclear-stain choice is invalid") from exc
    _validate_nuclear_component(nuclear, component_names, biomarkers)
    decision_source = value["decision_source"]
    if not _is_nonempty(decision_source):
        raise MetadataConflictError(
            f"Missing component identity decision source in {annotation.id}"
        )
    promoted = _parse_bool(
        value["channel_fluor_promoted"],
        "channel_fluor_promoted",
    )
    if promoted and mode is not ComponentIdentityMode.CHANNEL_FLUOROPHORES:
        raise MetadataConflictError(
            f"component identity promotion flag conflicts with mode in {annotation.id}"
        )
    return ComponentIdentityInventory(
        image_index=image_index,
        channel_ids=channel_ids,
        component_names=component_names,
        mode=mode,
        decision_source=decision_source.strip(),
        channel_fluor_promoted=promoted,
        component_biomarkers=biomarkers,
        nuclear_component_index=nuclear,
        nuclear_stain_reviewed=reviewed,
    )


def _validate_managed_annotations(annotations: Iterable[Any]) -> None:
    """

    Checks the formats and contents of annotation types defined by the pipeline.

    Choose the decoder from the record's namespace, then check its reserved image/channel
    ID. Channel annotations contain marker decisions, component annotations describe
    fluorescence identities, and acquisition annotations hold numerical settings.

    Args:
        annotations: Structured annotations to inspect.

    Raises:
        MetadataConflictError: A recognized annotation has an incompatible type,
            ID, format or contents.
    """

    for annotation in annotations:
        namespace = getattr(annotation, "namespace", None)
        if namespace == _UNMIXING_INPUT_V1_NAMESPACE:
            raise MetadataConflictError(
                "v1 unmixing input metadata requires source-backed migration"
            )
        if namespace == CHANNEL_METADATA_NAMESPACE:
            if not isinstance(annotation, MapAnnotation):
                raise MetadataConflictError(
                    "Managed channel metadata must be a MapAnnotation: "  f"{annotation.id}"
                )
            if _CHANNEL_ANNOTATION_ID.fullmatch(annotation.id) is None:
                raise MetadataConflictError(
                    "Managed channel annotation ID is not coordinate-indexed: "  f"{annotation.id}"
                )
            _annotation_assignment(annotation)
        elif namespace == INGESTION_PROVENANCE_NAMESPACE:
            if not isinstance(annotation, MapAnnotation):
                raise MetadataConflictError(
                    "Managed ingestion provenance must be a MapAnnotation: "  f"{annotation.id}"
                )
            _annotation_value(annotation)
        elif namespace == UNMIXING_INPUT_NAMESPACE:
            if not isinstance(annotation, MapAnnotation):
                raise MetadataConflictError(
                    "Managed unmixing input metadata must be a MapAnnotation: "  f"{annotation.id}"
                )
            match = _UNMIXING_INPUT_ANNOTATION_ID.fullmatch(annotation.id)
            if match is None:
                raise MetadataConflictError(
                    "Managed unmixing input annotation ID is not image-indexed: "  f"{annotation.id}"
                )
            _decode_unmixing_input_annotation(annotation, int(match["image"]))
        elif namespace == COMPONENT_IDENTITY_NAMESPACE:
            if not isinstance(annotation, MapAnnotation):
                raise MetadataConflictError(
                    "Managed component identity must be a MapAnnotation: "  f"{annotation.id}"
                )
            match = _COMPONENT_IDENTITY_ANNOTATION_ID.fullmatch(annotation.id)
            if match is None:
                raise MetadataConflictError(
                    "Managed component identity annotation ID is not image-indexed: "
                    f"{annotation.id}"
                )
            _decode_component_identity_annotation(annotation, int(match["image"]))


def _validate_managed_annotation_ownership(
    ome: OME,
    annotations_by_id: Mapping[str, Any],
) -> None:
    """

    Checks that each pipeline annotation is linked from the image or channel it describes.

    Build the actual image/channel positions, then walk all annotation-reference lists.
    A link to "Annotation:ChannelMetadata:0:1" must occur on that exact channel.
    Image-level acquisition, component and reader-version records follow their respective
    image-link rules. This prevents correctly formed data from being assigned to another image.

    Args:
        ome: Metadata tree containing the referring images and channels.
        annotations_by_id: Annotation lookup used to interpret each reference.

    Raises:
        MetadataConflictError: A reference is attached to the wrong object or position.
    """

    image_coordinates = {id(image): index for index, image in enumerate(ome.images)}
    channel_coordinates = {
        id(channel): (image_index, channel_index)
        for image_index, image in enumerate(ome.images)
        for channel_index, channel in enumerate(image.pixels.channels)
    }
    for owner, path, references in _iter_annotation_reference_owners(ome):
        owner_path = _format_model_path(path)
        for reference in references:
            annotation = annotations_by_id.get(reference.id)
            namespace = getattr(annotation, "namespace", None)
            channel_match = _CHANNEL_ANNOTATION_ID.fullmatch(reference.id)
            if namespace == CHANNEL_METADATA_NAMESPACE or channel_match is not None:
                if channel_match is None:
                    raise MetadataConflictError(
                        f"Managed channel annotation {reference.id} at {owner_path} "
                        "does not have a coordinate-indexed ID"
                    )
                expected = (
                    int(channel_match["image"]),
                    int(channel_match["channel"]),
                )
                if not isinstance(owner, Channel) or channel_coordinates.get(id(owner)) != expected:
                    raise MetadataConflictError(
                        f"Managed channel annotation {reference.id} at {owner_path} "
                        "may only be referenced by its coordinate-owned Channel "
                        f"at images[{expected[0]}].pixels.channels[{expected[1]}]"
                    )
                continue

            unmixing_match = _UNMIXING_INPUT_ANNOTATION_ID.fullmatch(reference.id)
            if namespace == UNMIXING_INPUT_NAMESPACE or unmixing_match is not None:
                if unmixing_match is None:
                    raise MetadataConflictError(
                        f"Managed unmixing annotation {reference.id} at {owner_path} "
                        "does not have an image-indexed ID"
                    )
                expected_image = int(unmixing_match["image"])
                if (
                    not isinstance(owner, Image)
                    or image_coordinates.get(id(owner)) != expected_image
                ):
                    raise MetadataConflictError(
                        f"Managed unmixing annotation {reference.id} at {owner_path} "
                        "may only be referenced by its coordinate-owned Image "
                        f"at images[{expected_image}]"
                    )
                continue

            component_match = _COMPONENT_IDENTITY_ANNOTATION_ID.fullmatch(reference.id)
            if namespace == COMPONENT_IDENTITY_NAMESPACE or component_match is not None:
                if component_match is None:
                    raise MetadataConflictError(
                        f"Managed component identity {reference.id} at {owner_path} "
                        "does not have an image-indexed ID"
                    )
                expected_image = int(component_match["image"])
                if (
                    not isinstance(owner, Image)
                    or image_coordinates.get(id(owner)) != expected_image
                ):
                    raise MetadataConflictError(
                        f"Managed component identity {reference.id} at {owner_path} "
                        "may only be referenced by its coordinate-owned Image "
                        f"at images[{expected_image}]"
                    )
                continue

            if (
                namespace == INGESTION_PROVENANCE_NAMESPACE
                or reference.id == _PROVENANCE_ANNOTATION_ID
            ) and not isinstance(owner, Image):
                raise MetadataConflictError(
                    f"Managed ingestion provenance {reference.id} at {owner_path} "
                    "may only be referenced by an Image"
                )


def _validate_component_identity_bindings(ome: OME) -> None:
    """

    Checks that every component review is linked once by its intended image and still
    matches that image's channel order and any asserted fluorophores.

    Args:
        ome: Metadata containing component-review records.

    Raises:
        MetadataConflictError: A reference is missing, repeated or misplaced, or
            reviewed components differ from the current channels.
    """

    annotations = _map_annotation_index(ome)
    for annotation in annotations.values():
        if annotation.namespace != COMPONENT_IDENTITY_NAMESPACE:
            continue
        match = _COMPONENT_IDENTITY_ANNOTATION_ID.fullmatch(annotation.id)
        assert match is not None
        expected_image = int(match["image"])
        link_counts = tuple(
            sum(reference.id == annotation.id for reference in image.annotation_refs)
            for image in ome.images
        )
        if any(
            count != (1 if image_index == expected_image else 0)
            for image_index, count in enumerate(link_counts)
        ):
            raise MetadataConflictError(
                f"Managed component identity {annotation.id} must be linked "
                "exactly once by its exact image"
            )
        resolve_component_identity_inventory(ome, expected_image)


def _is_nonempty(value: object) -> bool:
    """

    Checks whether a value contains text beyond whitespace.

    Args:
        value: Candidate text value.

    Returns:
        True for a string containing at least one non-whitespace character.
    """

    return isinstance(value, str) and bool(value.strip())


def _assignment_value(
    assignment: BiomarkerAssignment,
) -> tuple[Literal["assigned", "none"], str | None]:
    """

    Converts a researcher's marker choice into an explicit assignment state.

    Args:
        assignment: BiomarkerAssignment for a known image/channel position.

    Returns:
        ("none", None) for an intentional no-marker choice, otherwise
        ("assigned", trimmed_marker).

    Raises:
        MetadataCanonicalizationError: Supplied marker text is blank, non-text or
            uses the literal "None" token instead of the decoded no-marker value.
    """

    if assignment.biomarker is None:
        return "none", None
    if not isinstance(assignment.biomarker, str):
        raise MetadataCanonicalizationError(
            f"Biomarker must be text or None for image {assignment.image_index}, "
            f"channel {assignment.channel_index}"
        )
    biomarker = assignment.biomarker.strip()
    if not biomarker or biomarker == "None":
        raise MetadataCanonicalizationError(
            f"Invalid biomarker assignment for image {assignment.image_index}, "
            f"channel {assignment.channel_index}: {assignment.biomarker!r}"
        )
    return "assigned", biomarker


def _assignment_map_value(
    state: Literal["assigned", "none"],
    biomarker: str | None,
) -> dict[str, str]:
    """

    Creates the saved annotation fields for a checked marker decision.

    Args:
        state: "assigned" or "none".
        biomarker: Cleaned marker text for an assignment, otherwise None.

    Returns:
        A state field plus marker text when assigned, or the state field alone for "none".
    """

    if state == "none":
        return {"biomarker_assignment": "none"}
    assert biomarker is not None
    return {
        "biomarker_assignment": "assigned",
        "biomarker": biomarker,
    }


# ==============================================================================
#                       XML traversal and local tag names
# ==============================================================================


