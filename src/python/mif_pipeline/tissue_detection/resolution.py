"""

Selects the stored image resolution used to detect tissue regions.

An image pyramid contains the full-resolution image (level 0) and progressively
smaller copies. Reading one of these smaller images reduces the number of pixels
processed during tissue detection. Each level has a numbered position, a reduction
factor relative to level 0, and a physical pixel width and height in µm.

The researcher can choose a level directly, request a target pixel size, or review
the available levels in a prompt. The selected level and its calibration are saved
so subsequent tissue boundaries can be interpreted in full-resolution coordinates.
A read report then checks that QuPath used the requested level and reduction factor.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from math import isfinite

from mif_pipeline.pipeline_core.config import (
    NativeLevelSelectionMode,
    TissueDetectionConfig,
    TissueDetectionParameters,
)
from mif_pipeline.pipeline_core.errors import ProjectPreparationError

SELECTION_SCHEMA_VERSION = "coarse-tissue-resolution-selection/v1"
INTERACTION_CONTEXT_SCHEMA_VERSION = "tissue-native-resolution-interaction/v1"
_SELECTION_SOURCES = {"interactive", "configured_index", "automatic_target"}


# ==============================================================================
#                                   Resolution
# ==============================================================================


def _sha256(value: object) -> str:
    """

    Calculates a checksum for a resolution description after writing it as compact JSON.

    Sorting dictionary keys gives equivalent descriptions the same representation,
    allowing a saved level choice to be compared when the image is reopened.

    Args:
        value: Resolution fields that can be written as JSON.

    Returns:
        The 64-character SHA-256 checksum of the encoded description.
    """

    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _lowercase_sha256(value: object, field: str) -> str:
    """

    Checks that a saved checksum contains 64 lowercase hexadecimal characters.

    Args:
        value: Checksum text to check.
        field: Field name included in the error message.

    Returns:
        The valid checksum.

    Raises:
        ProjectPreparationError: The checksum has an invalid type, length or character.
    """

    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProjectPreparationError(f"{field} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True, slots=True)
class NativePyramidLevel:
    """

    Describes one stored resolution using its level number and physical pixel dimensions.

    The downsample factor states how much of the full-resolution image one pixel
    covers. A factor of 4 multiplies the full-resolution pixel width and height by 4.
    For a source with 0.5 µm-wide and 0.6 µm-high pixels, that level has 2.0 × 2.4 µm
    pixels. This is why physical width and height are retained separately even when
    the image is reduced equally along both axes.

    Attributes:
        native_resolution_index: Level number, beginning at 0 for full resolution.
        x_downsample: Horizontal reduction factor relative to level 0.
        y_downsample: Vertical reduction factor relative to level 0.
        x_um_per_px: Physical width of a pixel at this level, in µm.
        y_um_per_px: Physical height of a pixel at this level, in µm.
    """

    native_resolution_index: int
    x_downsample: float
    y_downsample: float
    x_um_per_px: float
    y_um_per_px: float

    def __post_init__(self) -> None:
        """

        Checks that the level number is nonnegative and each reduction factor and pixel
        size is finite and positive. These checks run when the level record is created.

        Raises:
            ProjectPreparationError: A level number or size is invalid.
        """

        if (
            isinstance(self.native_resolution_index, bool)
            or not isinstance(self.native_resolution_index, int)
            or self.native_resolution_index < 0
        ):
            raise ProjectPreparationError("native_resolution_index must be a non-negative integer")
        for field in (
            "x_downsample",
            "y_downsample",
            "x_um_per_px",
            "y_um_per_px",
        ):
            value = getattr(self, field)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
                or float(value) <= 0
            ):
                raise ProjectPreparationError(f"{field} must be a positive finite number")

    @classmethod
    def from_mapping(cls, value: object) -> "NativePyramidLevel":
        """

        Builds one level description from its five recorded fields.

        Args:
            value: Dictionary containing the level number, X/Y downsample factors and
                X/Y pixel sizes in µm.

        Returns:
            A NativePyramidLevel with checked field values.

        Raises:
            ProjectPreparationError: Required fields are missing, extra fields are present,
                or a level number or size is invalid.
        """

        if not isinstance(value, Mapping):
            raise ProjectPreparationError("native pyramid level must be a mapping")
        expected = {
            "native_resolution_index",
            "x_downsample",
            "y_downsample",
            "x_um_per_px",
            "y_um_per_px",
        }
        if set(value) != expected:
            raise ProjectPreparationError("native pyramid level requires exact resolution fields")
        return cls(**{field: value[field] for field in expected})

    @classmethod
    def sequence_from_mappings(cls, values: Sequence[object]) -> tuple["NativePyramidLevel", ...]:
        """

        Reads all stored image levels and checks their scales against the full-resolution image.

        Levels must be numbered consecutively from 0. For each level, the physical pixel
        width is the level-0 width multiplied by its X downsample factor; the same relation
        must hold for pixel height and the Y factor. The supported QuPath reader uses equal
        X/Y reduction factors, while the physical pixel dimensions can remain unequal.

        Successive levels must increase both pixel dimensions and downsample factors.
        Small numerical differences are allowed when checking calibration: the limit is
        the larger of 1e-9 µm and 1e-9 times the expected pixel size.

        Args:
            values: Level dictionaries in the order reported by the image reader.

        Returns:
            A tuple of checked NativePyramidLevel records.

        Raises:
            ProjectPreparationError: The level sequence is empty, numbering is incomplete,
                or the reported scales and pixel sizes are inconsistent.
        """

        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or not values:
            raise ProjectPreparationError("native pyramid inventory must be a non-empty sequence")
        levels = tuple(cls.from_mapping(value) for value in values)
        if tuple(item.native_resolution_index for item in levels) != tuple(range(len(levels))):
            raise ProjectPreparationError(
                "native pyramid resolution indices must be contiguous from level 0"
            )
        if abs(levels[0].x_downsample - 1.0) > 1e-9 or abs(levels[0].y_downsample - 1.0) > 1e-9:
            raise ProjectPreparationError("native pyramid level 0 must have x/y downsample 1")
        base_x_um = levels[0].x_um_per_px
        base_y_um = levels[0].y_um_per_px
        for item in levels:
            if abs(item.x_downsample - item.y_downsample) > 1e-9:
                raise ProjectPreparationError("QuPath native pyramid x/y downsamples must agree")
            expected_x_um = base_x_um * item.x_downsample
            expected_y_um = base_y_um * item.y_downsample
            if abs(item.x_um_per_px - expected_x_um) > max(1e-9, expected_x_um * 1e-9) or abs(
                item.y_um_per_px - expected_y_um
            ) > max(1e-9, expected_y_um * 1e-9):
                raise ProjectPreparationError("native pyramid physical calibration is inconsistent")
        for previous, current in zip(levels, levels[1:]):
            if (
                current.x_downsample <= previous.x_downsample
                or current.y_downsample <= previous.y_downsample
                or current.x_um_per_px <= previous.x_um_per_px
                or current.y_um_per_px <= previous.y_um_per_px
            ):
                raise ProjectPreparationError(
                    "native pyramid resolutions must be strictly increasing"
                )
        return levels

    def to_mapping(self) -> dict[str, object]:
        """

        Creates the level dictionary used in QuPath requests and saved selections.

        Returns:
            The level number and its four scale values, with scales written as floats.
        """

        return {
            "native_resolution_index": self.native_resolution_index,
            "x_downsample": float(self.x_downsample),
            "y_downsample": float(self.y_downsample),
            "x_um_per_px": float(self.x_um_per_px),
            "y_um_per_px": float(self.y_um_per_px),
        }


def native_pyramid_inventory_sha256(values: Sequence[object]) -> str:
    """

    Checks the available levels and calculates a checksum for their ordered descriptions.

    The checksum lets a saved selection detect changes to any level's scale or pixel
    size when the image is reopened. Accepted numerical sizes are written as floats
    before calculating it, giving values such as 4 and 4.0 the same representation.

    Args:
        values: Level dictionaries reported by the image reader.

    Returns:
        A SHA-256 checksum identifying the complete checked level list.

    Raises:
        ProjectPreparationError: A level description or its relationship to level 0
            is invalid.
    """

    levels = NativePyramidLevel.sequence_from_mappings(values)
    return _sha256([item.to_mapping() for item in levels])


# ==============================================================================
#                   Recording the selected native image level
# ==============================================================================


@dataclass(frozen=True, slots=True)
class TissueResolutionSelection:
    """

    Records the selected level, its calibration and how the researcher chose it.

    Tissue boundaries use pixel coordinates in the full-resolution image. Retaining
    the selected level's downsample factor allows the lower-resolution detection to
    be related to those coordinates. The two checksums identify the original level
    list and the completed selection so either can be checked on reopening.

    Attributes:
        schema_version: Format identifier for this selection record.
        selection_source: "interactive", "configured_index" or "automatic_target".
        native_pyramid_inventory_sha256: Checksum of the available image levels.
        native_resolution_index: Number of the selected level.
        x_downsample: Selected level's horizontal reduction factor.
        y_downsample: Selected level's vertical reduction factor.
        x_um_per_px: Pixel width at the selected level, in µm.
        y_um_per_px: Pixel height at the selected level, in µm.
        resolution_source: "native_pyramid", identifying a stored image level.
        coordinate_system: "global_level0_pixels", the tissue-coordinate convention.
        level0_opt_in: Additional confirmation for an interactive full-resolution
            choice. Direct and automatic choices use the permission in their settings.
        selection_record_sha256: Checksum of the other selection fields.
    """

    schema_version: str
    selection_source: str
    native_pyramid_inventory_sha256: str
    native_resolution_index: int
    x_downsample: float
    y_downsample: float
    x_um_per_px: float
    y_um_per_px: float
    resolution_source: str
    coordinate_system: str
    level0_opt_in: bool
    selection_record_sha256: str

    def __post_init__(self) -> None:
        """

        Checks the selection's scale, coordinate convention and full-resolution confirmation,
        then recalculates its checksum from the recorded fields.

        Raises:
            ProjectPreparationError: A field is invalid, an interactive level-0 selection
                lacks confirmation, or the saved checksum differs from the record contents.
        """

        if self.schema_version != SELECTION_SCHEMA_VERSION:
            raise ProjectPreparationError("tissue resolution selection schema is invalid")
        if self.selection_source not in _SELECTION_SOURCES:
            raise ProjectPreparationError("tissue resolution selection_source is invalid")
        _lowercase_sha256(
            self.native_pyramid_inventory_sha256,
            "native_pyramid_inventory_sha256",
        )
        NativePyramidLevel(
            native_resolution_index=self.native_resolution_index,
            x_downsample=self.x_downsample,
            y_downsample=self.y_downsample,
            x_um_per_px=self.x_um_per_px,
            y_um_per_px=self.y_um_per_px,
        )
        if abs(self.x_downsample - self.y_downsample) > 1e-9:
            raise ProjectPreparationError("selected native x/y downsamples must agree")
        if self.resolution_source != "native_pyramid":
            raise ProjectPreparationError("selected resolution_source must be native_pyramid")
        if self.coordinate_system != "global_level0_pixels":
            raise ProjectPreparationError("selected coordinate_system must be global_level0_pixels")
        if not isinstance(self.level0_opt_in, bool):
            raise ProjectPreparationError("level0_opt_in must be a boolean")
        if (
            self.selection_source == "interactive"
            and self.native_resolution_index == 0
            and not self.level0_opt_in
        ):
            raise ProjectPreparationError("interactive level 0 selection requires recorded opt-in")
        if self.native_resolution_index > 0 and self.level0_opt_in:
            raise ProjectPreparationError("level0_opt_in must be false for a lower native level")
        expected_digest = _sha256(self._digest_payload())
        if self.selection_record_sha256 != expected_digest:
            raise ProjectPreparationError("tissue resolution selection record digest is invalid")

    @classmethod
    def create(
        cls,
        *,
        selection_source: str,
        inventory: Sequence[object],
        selected: NativePyramidLevel,
        level0_opt_in: bool,
    ) -> "TissueResolutionSelection":
        """

        Creates the saved selection after a level has been chosen.

        This combines the level's calibration with the selection method and full-resolution
        confirmation, then calculates checksums for the available levels and completed record.
        The selector supplies the chosen level from the same list.

        Args:
            selection_source: Interactive, direct-index or automatic-target selection.
            inventory: Available level dictionaries used for this choice.
            selected: The NativePyramidLevel chosen from those levels.
            level0_opt_in: Explicit full-resolution confirmation from an interactive prompt.

        Returns:
            A TissueResolutionSelection ready to be recorded with the detector.
        """

        values = {
            "schema_version": SELECTION_SCHEMA_VERSION,
            "selection_source": selection_source,
            "native_pyramid_inventory_sha256": native_pyramid_inventory_sha256(inventory),
            **selected.to_mapping(),
            "resolution_source": "native_pyramid",
            "coordinate_system": "global_level0_pixels",
            "level0_opt_in": level0_opt_in,
        }
        return cls(**values, selection_record_sha256=_sha256(values))

    @classmethod
    def from_mapping(cls, value: object) -> "TissueResolutionSelection":
        """

        Reconstructs a saved selection and checks its fields and checksum.

        Args:
            value: Dictionary containing the twelve selection fields.

        Returns:
            The checked TissueResolutionSelection.

        Raises:
            ProjectPreparationError: Fields are missing or unexpected, values are invalid,
                or the recorded checksum does not match the selection.
        """

        if not isinstance(value, Mapping):
            raise ProjectPreparationError("tissue resolution selection must be a mapping")
        expected = {
            "schema_version",
            "selection_source",
            "native_pyramid_inventory_sha256",
            "native_resolution_index",
            "x_downsample",
            "y_downsample",
            "x_um_per_px",
            "y_um_per_px",
            "resolution_source",
            "coordinate_system",
            "level0_opt_in",
            "selection_record_sha256",
        }
        if set(value) != expected:
            raise ProjectPreparationError("tissue resolution selection requires exact fields")
        return cls(**{field: value[field] for field in expected})

    def _digest_payload(self) -> dict[str, object]:
        """

        Collects the selection fields used to calculate its checksum.

        Scale values are written as floats so equivalent numerical values produce the
        same checksum. The checksum itself is added afterward by to_mapping().

        Returns:
            A dictionary containing the selection method, level, scales, coordinate
            convention, full-resolution confirmation and level-list checksum.
        """

        return {
            "schema_version": self.schema_version,
            "selection_source": self.selection_source,
            "native_pyramid_inventory_sha256": self.native_pyramid_inventory_sha256,
            "native_resolution_index": self.native_resolution_index,
            "x_downsample": float(self.x_downsample),
            "y_downsample": float(self.y_downsample),
            "x_um_per_px": float(self.x_um_per_px),
            "y_um_per_px": float(self.y_um_per_px),
            "resolution_source": self.resolution_source,
            "coordinate_system": self.coordinate_system,
            "level0_opt_in": self.level0_opt_in,
        }

    def to_mapping(self) -> dict[str, object]:
        """

        Builds the complete dictionary written to the saved analysis or a QuPath request.

        Returns:
            The selection fields and their checksum.
        """

        return {
            **self._digest_payload(),
            "selection_record_sha256": self.selection_record_sha256,
        }

    def validate_inventory_binding(self, values: Sequence[object]) -> None:
        """

        Checks that the saved choice still refers to the same image levels and calibration.

        First compare the checksum of the current level list with the saved list. Then
        check the chosen level's number, reduction factors and physical pixel sizes.
        This catches a changed reader configuration even when its level numbers look familiar.

        Args:
            values: Current level dictionaries reported by the image reader.

        Raises:
            ProjectPreparationError: The available levels have changed, the selected level
                is absent, or its calibration differs from the saved selection.
        """

        levels = NativePyramidLevel.sequence_from_mappings(values)
        if (
            native_pyramid_inventory_sha256(tuple(item.to_mapping() for item in levels))
            != self.native_pyramid_inventory_sha256
        ):
            raise ProjectPreparationError("tissue resolution selection native inventory changed")
        selected = (
            levels[self.native_resolution_index]
            if self.native_resolution_index < len(levels)
            else None
        )
        if selected is None or selected.to_mapping() != {
            "native_resolution_index": self.native_resolution_index,
            "x_downsample": float(self.x_downsample),
            "y_downsample": float(self.y_downsample),
            "x_um_per_px": float(self.x_um_per_px),
            "y_um_per_px": float(self.y_um_per_px),
        }:
            raise ProjectPreparationError(
                "tissue resolution selection does not match native inventory"
            )


# ==============================================================================
#                    Checking the reported native image read
# ==============================================================================


@dataclass(frozen=True, slots=True)
class NativeLevelReadAttestation:
    """

    Stores QuPath's report of the image region it read for tissue detection.

    The level and requested downsample are compared with the saved selection before
    the result is used. Returned width and height describe the lower-resolution
    pixel array; associated tissue coordinates remain in full-resolution pixels.

    Attributes:
        native_resolution_index: Number of the level QuPath reports using.
        requested_downsample: Reduction factor requested for the read.
        returned_width_px: Width of the returned pixel array.
        returned_height_px: Height of the returned pixel array.
        resolution_source: "native_pyramid" for the stored-level read.
        coordinate_system: "global_level0_pixels" for associated coordinates.
        selection_record_sha256: Checksum identifying the requested selection.
    """

    native_resolution_index: int
    requested_downsample: float
    returned_width_px: int
    returned_height_px: int
    resolution_source: str
    coordinate_system: str
    selection_record_sha256: str

    def __post_init__(self) -> None:
        """

        Checks the reported level number, positive returned dimensions and downsample,
        then checks the coordinate labels and selection-checksum format.

        Raises:
            ProjectPreparationError: A read-report field is invalid.
        """

        for field in (
            "native_resolution_index",
            "returned_width_px",
            "returned_height_px",
        ):
            value = getattr(self, field)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < (0 if field == "native_resolution_index" else 1)
            ):
                raise ProjectPreparationError(f"{field} must be a valid non-boolean integer")
        if (
            isinstance(self.requested_downsample, bool)
            or not isinstance(self.requested_downsample, (int, float))
            or not isfinite(float(self.requested_downsample))
            or float(self.requested_downsample) <= 0
        ):
            raise ProjectPreparationError("requested_downsample must be a positive finite number")
        if self.resolution_source != "native_pyramid":
            raise ProjectPreparationError("native read resolution_source must be native_pyramid")
        if self.coordinate_system != "global_level0_pixels":
            raise ProjectPreparationError(
                "native read coordinate_system must be global_level0_pixels"
            )
        _lowercase_sha256(self.selection_record_sha256, "selection_record_sha256")

    @classmethod
    def from_mapping(cls, value: object) -> "NativeLevelReadAttestation":
        """

        Reads the seven fields in QuPath's image-read report and validates their values.

        Args:
            value: Dictionary containing the level, downsample, returned dimensions,
                coordinate labels and selection checksum.

        Returns:
            A checked NativeLevelReadAttestation.

        Raises:
            ProjectPreparationError: Fields are missing, unexpected or invalid.
        """

        if not isinstance(value, Mapping):
            raise ProjectPreparationError("native level read attestation must be a mapping")
        expected = {
            "native_resolution_index",
            "requested_downsample",
            "returned_width_px",
            "returned_height_px",
            "resolution_source",
            "coordinate_system",
            "selection_record_sha256",
        }
        if set(value) != expected:
            raise ProjectPreparationError("native level read attestation requires exact fields")
        return cls(**{field: value[field] for field in expected})

    def validate_selection_binding(self, selection: TissueResolutionSelection) -> None:
        """

        Compares the reported read with the level selection sent to QuPath.

        The level number, requested downsample, coordinate labels and selection checksum
        must all agree. Comparing the requested downsample with both saved axes ensures
        that the read uses the selected spatial scale.

        Args:
            selection: The tissue-resolution selection supplied with the read request.

        Raises:
            ProjectPreparationError: QuPath's report identifies a different selection or scale.
        """

        if (
            self.native_resolution_index != selection.native_resolution_index
            or self.requested_downsample != selection.x_downsample
            or self.requested_downsample != selection.y_downsample
            or self.resolution_source != selection.resolution_source
            or self.coordinate_system != selection.coordinate_system
            or self.selection_record_sha256 != selection.selection_record_sha256
        ):
            raise ProjectPreparationError(
                "native level read attestation does not match the selected request"
            )

    def to_mapping(self) -> dict[str, object]:
        """

        Creates the dictionary used to save or transmit QuPath's read report.

        Returns:
            The reported level, downsample, pixel-array dimensions and selection identity.
        """

        return {
            "native_resolution_index": self.native_resolution_index,
            "requested_downsample": float(self.requested_downsample),
            "returned_width_px": self.returned_width_px,
            "returned_height_px": self.returned_height_px,
            "resolution_source": self.resolution_source,
            "coordinate_system": self.coordinate_system,
            "selection_record_sha256": self.selection_record_sha256,
        }


# ==============================================================================
#                Resolving the requested image-resolution policy
# ==============================================================================


def _unique_target_match(
    config: TissueDetectionParameters,
    levels: Sequence[NativePyramidLevel],
) -> NativePyramidLevel:
    """

    Finds the single native image pyramid level whose pixel width (X) and height (Y) both lie within the target interval.

    We calculate the allowed deviation from the target resolution by multiplying
    that resolution by the tolerance fraction. The resulting target interval is
    [target - tolerance, target + tolerance]. For example, a target of 8 µm/pixel
    with a tolerance fraction of 0.05 gives an interval of 7.6 to 8.4 µm/pixel.

    This function checks both physical pixel sizes of each level by calculating
    whether their deviation from the target satisfies:

        abs(level_pixel_size - target) <= tolerance

    Exactly one level must satisfy this condition on both axes before we return
    the selected resolution for tissue detection.

    Args:
        config: Tissue settings containing the target resolution in µm/pixel
            and allowed deviation expressed as a tolerance fraction.
        levels: Sequence of native image pyramid levels available for selection.

    Returns:
        Single native level whose X and Y pixel sizes both satisfy the target interval.

    Raises:
        ProjectPreparationError: Zero levels qualify, or multiple levels satisfy
            the target interval at the same time.
    """

    # Convert the fractional tolerance to the allowed deviation in µm/pixel.
    target = config.resolution_um_per_px
    tolerance = target * config.native_level_match_tolerance_fraction

    # For each level, check pixel width (X) and height (Y) against the allowed deviation.
    matches = tuple(
        item
        for item in levels
        if abs(item.x_um_per_px - target) <= tolerance
        and abs(item.y_um_per_px - target) <= tolerance
    )
    if not matches:
        raise ProjectPreparationError(
            "configured tissue resolution has no native pyramid level within the explicit tolerance"
        )
    if len(matches) != 1:
        raise ProjectPreparationError(
            "configured tissue resolution matches multiple native pyramid levels"
        )
    return matches[0]


def permitted_tissue_native_resolution_indices(
    parameters: TissueDetectionParameters,
    native_pyramid_levels: Sequence[object],
) -> tuple[int, ...]:
    """

    Determines which stored levels can be used under the chosen tissue-detection settings.

    For an interactive choice, list the smaller image levels and include level 0 only
    when full-resolution processing is enabled. A direct-index choice selects its named
    level. An automatic choice finds the single level whose X/Y pixel sizes satisfy
    the target range. Every route requires at least one usable level.

    Args:
        parameters: Tissue detector settings containing the selection method, target
            or index, and full-resolution permission.
        native_pyramid_levels: Available level dictionaries from the image reader.

    Returns:
        A tuple of permitted level numbers.

    Raises:
        ProjectPreparationError: The settings or levels are invalid, the named level
            is unavailable, the target has no unique match, or level 0 lacks permission.
    """

    if type(parameters) not in (TissueDetectionParameters, TissueDetectionConfig):
        raise ProjectPreparationError("tissue feasibility requires tissue parameters")
    mapping = parameters.to_mapping()
    mapping.pop("classifier_sha256", None)
    parameters = TissueDetectionParameters.from_mapping(mapping, field="tissue_detection")
    levels = NativePyramidLevel.sequence_from_mappings(native_pyramid_levels)
    mode = parameters.native_level_selection_mode
    if mode is NativeLevelSelectionMode.PROMPT:
        indices = tuple(
            item.native_resolution_index
            for item in levels
            if item.native_resolution_index > 0 or parameters.allow_level0_selection
        )
    else:
        index = (
            parameters.native_resolution_index
            if mode is NativeLevelSelectionMode.CONFIGURED_INDEX
            else _unique_target_match(parameters, levels).native_resolution_index
        )
        if index is None or index >= len(levels):
            raise ProjectPreparationError("configured native_resolution_index is unavailable")
        if index == 0 and not parameters.allow_level0_selection:
            raise ProjectPreparationError(
                "level-0 tissue selection is not authorized by configuration"
            )
        indices = (index,)
    if not indices:
        raise ProjectPreparationError("no authorized native tissue resolution is available")
    return indices


def build_tissue_resolution_interaction_context(
    config: TissueDetectionConfig,
    native_pyramid_levels: Sequence[object],
) -> dict[str, object]:
    """

    Prepares the level choices and a suggested resolution for the researcher's prompt.

    A smaller image level is recommended when exactly one such level has X/Y pixel
    sizes within the configured target range. Otherwise the recommendation is left
    empty so the researcher can choose from the displayed pixel sizes. The prompt
    also carries the permission to use full resolution and the level-list checksum.

    Args:
        config: Detector settings containing the target resolution, fractional
            deviation and full-resolution permission.
        native_pyramid_levels: Available level dictionaries to display.

    Returns:
        The prompt's level descriptions, optional recommendation, target settings
        and an initially empty read-report field.
    """

    levels = NativePyramidLevel.sequence_from_mappings(native_pyramid_levels)
    target = config.resolution_um_per_px
    tolerance = target * config.native_level_match_tolerance_fraction
    lower_matches = tuple(
        item
        for item in levels
        if item.native_resolution_index > 0
        and abs(item.x_um_per_px - target) <= tolerance
        and abs(item.y_um_per_px - target) <= tolerance
    )
    recommended = lower_matches[0].native_resolution_index if len(lower_matches) == 1 else None
    inventory = [item.to_mapping() for item in levels]
    return {
        "schema_version": INTERACTION_CONTEXT_SCHEMA_VERSION,
        "native_pyramid_inventory": inventory,
        "native_pyramid_inventory_sha256": _sha256(inventory),
        "recommended_native_resolution_index": recommended,
        "target_resolution_um_per_px": float(target),
        "native_level_match_tolerance_fraction": float(
            config.native_level_match_tolerance_fraction
        ),
        "allow_level0_selection": config.allow_level0_selection,
        "native_level_read_attestation": None,
    }


def select_tissue_native_resolution(
    config: TissueDetectionConfig,
    native_pyramid_levels: Sequence[object],
    *,
    response: Mapping[str, object] | None = None,
) -> TissueResolutionSelection:
    """

    Resolves the interactive, direct-index or automatic choice into one saved level selection.

    An interactive response supplies the level number and any full-resolution
    confirmation. Direct and automatic choices use the saved settings. After resolving
    the number, check that the level exists and that level 0 is permitted. The completed
    record includes the chosen pixel sizes, reduction factors and level-list checksum.

    Args:
        config: Accepted detector settings specifying how to select the resolution.
        native_pyramid_levels: Available level dictionaries from the current image.
        response: Interactive response containing native_resolution_index and
            level0_opt_in. Supply None for direct or automatic selection.

    Returns:
        A TissueResolutionSelection describing the chosen stored image level.

    Raises:
        ProjectPreparationError: The response disagrees with the selection method,
            a level is unavailable, the automatic match is ambiguous, or required
            full-resolution permission/confirmation is missing.
    """

    if not isinstance(config, TissueDetectionConfig):
        raise ProjectPreparationError("tissue resolution selection requires TissueDetectionConfig")
    levels = NativePyramidLevel.sequence_from_mappings(native_pyramid_levels)
    mode = config.native_level_selection_mode
    if mode is NativeLevelSelectionMode.PROMPT:
        if response is None:
            raise ProjectPreparationError("prompt tissue resolution requires a submitted index")
        if not isinstance(response, Mapping) or set(response) != {
            "native_resolution_index",
            "level0_opt_in",
        }:
            raise ProjectPreparationError(
                "prompt tissue resolution response requires exact index and opt-in fields"
            )
        index = response["native_resolution_index"]
        opt_in = response["level0_opt_in"]
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ProjectPreparationError("native_resolution_index must be a non-negative integer")
        if not isinstance(opt_in, bool):
            raise ProjectPreparationError("level0_opt_in must be a boolean")
        source = "interactive"
    else:
        if response is not None:
            raise ProjectPreparationError(
                "configured tissue resolution selection does not accept a response"
            )
        opt_in = False
        if mode is NativeLevelSelectionMode.CONFIGURED_INDEX:
            assert config.native_resolution_index is not None
            index = config.native_resolution_index
            source = "configured_index"
        elif mode is NativeLevelSelectionMode.AUTOMATIC_TARGET:
            selected_match = _unique_target_match(config, levels)
            index = selected_match.native_resolution_index
            source = "automatic_target"
        else:  # pragma: no cover - frozen config rejects this state
            raise ProjectPreparationError("native level selection mode is invalid")
    if index >= len(levels):
        raise ProjectPreparationError("selected native_resolution_index is unavailable")

    # Full-resolution processing must be permitted by the settings. The
    # interactive route additionally records the submitted confirmation.
    if index == 0:
        if not config.allow_level0_selection:
            raise ProjectPreparationError(
                "level-0 tissue selection is not authorized by configuration"
            )
        if mode is NativeLevelSelectionMode.PROMPT and not opt_in:
            raise ProjectPreparationError(
                "interactive level-0 tissue selection requires explicit opt-in"
            )
    elif opt_in:
        raise ProjectPreparationError("level0_opt_in must be false for a lower native level")
    inventory = tuple(item.to_mapping() for item in levels)
    return TissueResolutionSelection.create(
        selection_source=source,
        inventory=inventory,
        selected=levels[index],
        level0_opt_in=opt_in,
    )


__all__ = [
    "NativeLevelReadAttestation",
    "NativePyramidLevel",
    "SELECTION_SCHEMA_VERSION",
    "TissueResolutionSelection",
    "build_tissue_resolution_interaction_context",
    "native_pyramid_inventory_sha256",
    "permitted_tissue_native_resolution_indices",
    "select_tissue_native_resolution",
]
