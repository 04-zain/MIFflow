"""

Prepares a detector-inspection request and checks QuPath's reply.

A saved detector must use the selected image channel and a pixel size available
in the image pyramid. Python sends the chosen detector, channel and permitted
levels to QuPath. QuPath opens the model and reports its labels, input calibration
and compatible levels. The reply is compared with the request before the detector's
checksum is added to the accepted tissue configuration.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import isfinite
from pathlib import Path
import re

from mif_pipeline.pipeline_core.config import (
    ChannelSpec,
    TissueDetectionConfig,
    TissueDetectionParameters,
    validate_tissue_classifier_name,
)
from mif_pipeline.pipeline_core.errors import ProjectPreparationError, ROIAnalysisError
from mif_pipeline.tissue_detection.resolution import (
    NativePyramidLevel,
    permitted_tissue_native_resolution_indices,
)

MAX_TISSUE_CLASSIFIER_BYTES = 64 * 1024 * 1024
_REQUEST_KEYS = {
    "schema_version",
    "action",
    "classifier_name",
    "expected_classifier_sha256",
    "source",
    "reference_channel_id",
    "channel_index",
    "channel_name",
    "native_pyramid_inventory",
    "permitted_native_resolution_indices",
    "channels",
}
_OBSERVATION_KEYS = {
    "schema_version",
    "classifier_name",
    "classifier_sha256",
    "classifier_byte_count",
    "source",
    "classifier_type",
    "reference_channel_id",
    "channel_index",
    "channel_name",
    "input_resolution_um",
    "classification_labels",
    "compatible_native_resolution_indices",
}


# ==============================================================================
#                                     Setup
# ==============================================================================


def tissue_setup_authoring_seed(*, validation_domain_id: str) -> dict[str, object]:
    """

    Builds the initial settings for a tissue-detector review.

    Numerical values come from the tissue presets. The detector name, reference channel
    and final level remain available for the researcher to choose. The processing-group
    identifier connects the resulting settings to the intended analysis group.

    Args:
        validation_domain_id: Nonempty processing-group identifier.

    Returns:
        A dictionary of starting tissue settings.

    Raises:
        ProjectPreparationError: The processing-group identifier is missing.
    """

    if not isinstance(validation_domain_id, str) or not validation_domain_id.strip():
        raise ProjectPreparationError("tissue setup requires its processing group")
    from mif_pipeline._defaults import default_section

    defaults = default_section("tissue_detection")
    return {
        "detector_id": "qupath-pixel-classifier",
        "detector_version": "1",
        "stage_version": "tissue-detection/v1",
        "validation_domain_id": validation_domain_id,
        "classifier_name": None,
        "reference_channel_id": None,
        "reference_channel_selection_source": None,
        "native_level_selection_mode": "prompt",
        "native_resolution_index": None,
        **defaults,
    }


def _closed(value: object, keys: set[str], field: str) -> Mapping:
    """

    Checks that a dictionary contains exactly the fields required by a request or reply.

    Args:
        value: Dictionary to inspect.
        keys: Required field names.
        field: Record name used in an error message.

    Returns:
        The checked dictionary.

    Raises:
        ProjectPreparationError: Fields are missing or unexpected, or value is not a mapping.
    """

    if not isinstance(value, Mapping) or set(value) != keys:
        raise ProjectPreparationError(f"{field} requires exact fields: {", ".join(sorted(keys))}")
    return value


def _integer(value: object, field: str, *, minimum: int = 0, maximum: int = 2**31 - 1) -> int:
    """

    Checks a count or index against its permitted integer range.

    Args:
        value: Number to check; booleans are rejected.
        field: Field name used in an error message.
        minimum: Lowest permitted integer, inclusive.
        maximum: Highest permitted integer, inclusive.

    Returns:
        The valid integer.

    Raises:
        ProjectPreparationError: The value has the wrong type or lies outside the range.
    """

    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ProjectPreparationError(f"{field} must be an integer from {minimum} to {maximum}")
    return value


def _digest(value: object, field: str) -> str:
    """

    Checks the lowercase hexadecimal format of a saved SHA-256 checksum.

    Args:
        value: Expected 64-character checksum text.
        field: Field name used in an error message.

    Returns:
        The valid checksum.

    Raises:
        ProjectPreparationError: The checksum format is invalid.
    """

    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ProjectPreparationError(f"{field} must be a lowercase SHA-256")
    return value


def _sequence(value: object, field: str) -> Sequence:
    """

    Requires a nonempty sequence of records or values.

    Args:
        value: Sequence to inspect; strings and bytes are rejected as list substitutes.
        field: Sequence name used in an error message.

    Returns:
        The checked sequence.

    Raises:
        ProjectPreparationError: The sequence is empty or has an unsupported type.
    """

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ProjectPreparationError(f"{field} must be a nonempty sequence")
    return value


def _source(value: object, *, include_path: bool) -> dict[str, object]:
    """

    Checks the file description accompanying a detector import.

    The request includes the absolute source path, checksum and byte count. QuPath's
    reply reports the checksum and byte count so they can be compared with the selected
    file. The same helper checks these two representations using include_path.

    Args:
        value: Detector-file description from the request or reply.
        include_path: Whether the source path is required alongside size and checksum.

    Returns:
        The validated source-file fields.

    Raises:
        ProjectPreparationError: Fields, checksum, file size or path format are invalid.
    """

    keys = {"sha256", "byte_count"} | ({"path"} if include_path else set())
    value = _closed(value, keys, "source")
    result = {
        "sha256": _digest(value["sha256"], "source.sha256"),
        "byte_count": _integer(
            value["byte_count"], "source.byte_count", minimum=1, maximum=MAX_TISSUE_CLASSIFIER_BYTES
        ),
    }
    if include_path:
        path = value["path"]
        if (
            not isinstance(path, str)
            or "\0" in path
            or not Path(path).is_absolute()
            or ".." in Path(path).parts
            or str(Path(path)) != path
        ):
            raise ProjectPreparationError("source.path must be an absolute normalized local path")
        result["path"] = path
    return result


def _parameters(value: object) -> tuple[TissueDetectionParameters, str | None]:
    """

    Checks tissue settings and separates any already accepted detector checksum.

    Round-trip the settings through their normal validator before building a request.
    An accepted configuration includes a detector checksum; initial parameters do not.
    Separating that checksum allows both to use the same detector-inspection path.

    Args:
        value: TissueDetectionParameters or TissueDetectionConfig to inspect.

    Returns:
        Validated parameters followed by the existing detector checksum, or None
        when the detector has not yet been accepted.

    Raises:
        ProjectPreparationError: The settings type or recorded values are invalid.
    """

    if type(value) not in (TissueDetectionParameters, TissueDetectionConfig):
        raise ProjectPreparationError(
            "parameters must be TissueDetectionParameters or TissueDetectionConfig"
        )
    try:
        parsed = type(value).from_mapping(value.to_mapping(), field="tissue_detection")
    except ROIAnalysisError as exc:
        raise ProjectPreparationError(str(exc)) from exc
    if parsed != value:
        raise ProjectPreparationError("tissue parameters must already be canonical")
    mapping = parsed.to_mapping()
    expected = mapping.pop("classifier_sha256", None)
    return TissueDetectionParameters.from_mapping(mapping, field="tissue_detection"), expected


def _channels(values: object) -> tuple[ChannelSpec, ...]:
    """

    Checks the ordered image channels used to identify the detector's input.

    Each ChannelSpec is reconstructed through its validator, then channel IDs are
    checked for duplicates. The order determines the numerical channel index sent
    to QuPath.

    Args:
        values: Nonempty sequence of ChannelSpec records.

    Returns:
        The validated channels as a tuple.

    Raises:
        ProjectPreparationError: A channel record is invalid or channel IDs repeat.
    """

    result = []
    for item in _sequence(values, "channels"):
        if type(item) is not ChannelSpec:
            raise ProjectPreparationError("channels require ordered ChannelSpec records")
        try:
            parsed = ChannelSpec.from_mapping(item.to_mapping(), field="channel")
        except ROIAnalysisError as exc:
            raise ProjectPreparationError(str(exc)) from exc
        if parsed != item:
            raise ProjectPreparationError("channels must already be canonical")
        result.append(parsed)
    if len({item.channel_id for item in result}) != len(result):
        raise ProjectPreparationError("channels must have unique channel IDs")
    return tuple(result)


# ==============================================================================
#                     Preparing the native detector request
# ==============================================================================


def build_tissue_setup_request(
    *,
    parameters: TissueDetectionParameters | TissueDetectionConfig | None,
    channels: Sequence[ChannelSpec],
    native_pyramid_inventory: Sequence[object],
    source: Mapping[str, object] | None = None,
    expected_classifier_sha256: str | None = None,
) -> dict[str, object]:
    """

    Builds the request for selecting, inspecting or importing a tissue detector.

    An unresolved setup lists the image channels and levels for selection. Once the
    researcher has chosen settings, resolve the reference channel's ID to its numerical
    position and name, determine the permitted levels, and include either a project
    detector or a local import source.

    For an already accepted detector, its recorded checksum is included so reopening
    checks the same model. Import requests instead carry the selected source file's
    size and checksum.

    Args:
        parameters: Selected or accepted tissue settings; None requests initial selection.
        channels: Ordered channels of the image being analysed.
        native_pyramid_inventory: Stored image levels and their physical calibration.
        source: Checked local file description for an import, otherwise None.
        expected_classifier_sha256: Optional checksum of an already saved detector.

    Returns:
        The complete request dictionary supplied to the native QuPath script.

    Raises:
        ProjectPreparationError: Settings, source information, channel selection
            or available resolutions are inconsistent.
    """

    channels = _channels(channels)
    levels = NativePyramidLevel.sequence_from_mappings(native_pyramid_inventory)
    request = dict.fromkeys(_REQUEST_KEYS)
    request.update(
        schema_version="tissue-detector-setup-request/v1",
        action="request_selection",
        channels=[item.to_mapping() for item in channels],
        native_pyramid_inventory=[item.to_mapping() for item in levels],
    )
    if parameters is None:
        if source is not None or expected_classifier_sha256 is not None:
            raise ProjectPreparationError(
                "unresolved tissue parameters cannot supply source or expected hash"
            )
        return request
    parameters, concrete_hash = _parameters(parameters)
    if expected_classifier_sha256 is not None:
        _digest(expected_classifier_sha256, "expected_classifier_sha256")
    if concrete_hash is not None:
        if expected_classifier_sha256 not in (None, concrete_hash):
            raise ProjectPreparationError(
                "expected saved classifier hash conflicts with concrete config"
            )
        expected_classifier_sha256 = concrete_hash
    name = validate_tissue_classifier_name(parameters.classifier_name)
    index = next(
        (
            i
            for i, channel in enumerate(channels)
            if channel.channel_id == parameters.reference_channel_id
        ),
        None,
    )
    if index is None or channels[index].name is None:
        raise ProjectPreparationError(
            "reference channel requires its explicit canonical ID and nonempty name"
        )
    if source is not None and expected_classifier_sha256 is not None:
        raise ProjectPreparationError(
            "import source cannot supply an expected saved classifier hash"
        )
    request.update(
        action="observe_project" if source is None else "import_local",
        classifier_name=name,
        expected_classifier_sha256=expected_classifier_sha256,
        source=None if source is None else _source(source, include_path=True),
        reference_channel_id=parameters.reference_channel_id,
        channel_index=index,
        channel_name=channels[index].name,
        permitted_native_resolution_indices=list(
            permitted_tissue_native_resolution_indices(
                parameters, request["native_pyramid_inventory"]
            )
        ),
    )
    return request


def _indices(value: object, field: str) -> list[int]:
    """

    Checks that reported image-level numbers are nonnegative, sorted and unique.

    Args:
        value: Nonempty sequence of level numbers.
        field: Name of the level-list field used in errors.

    Returns:
        The validated level numbers as a list.

    Raises:
        ProjectPreparationError: An index is invalid, repeated or out of order.
    """

    result = [_integer(item, field) for item in _sequence(value, field)]
    if result != sorted(set(result)):
        raise ProjectPreparationError(f"{field} must be sorted and unique")
    return result


# ==============================================================================
#                       Checking the native detector reply
# ==============================================================================


def resolve_tissue_setup_observation(
    *,
    parameters: TissueDetectionParameters | TissueDetectionConfig,
    request: Mapping[str, object],
    observation: Mapping[str, object],
) -> TissueDetectionConfig:
    """

    Checks QuPath's inspected detector against the selected settings and completes its configuration.

    First rebuild the request from the settings to confirm that the reply belongs to
    the intended detector, channel and levels. Compare the reported model checksum
    and import source with the request. The model must be a supported threshold or
    OpenCV pixel classifier with at least one named, nonignored foreground label.

    The detector's input pixel width and height must match the calibration of at least
    one permitted image level. QuPath's compatible-level list must equal the list
    calculated from those dimensions; agreement is checked to 1e-9 µm on each axis.
    The accepted detector checksum is then added to the tissue settings.

    Args:
        parameters: Selected or previously accepted tissue settings.
        request: Exact request sent to QuPath for detector inspection.
        observation: QuPath's detector type, labels, file identity and input calibration.

    Returns:
        A TissueDetectionConfig containing the inspected detector's checksum.

    Raises:
        ProjectPreparationError: The reply differs from the request, the detector
            labels/type are unsuitable, or its pixel calibration matches no permitted level.
    """

    request = _closed(request, _REQUEST_KEYS, "tissue setup request")
    if parameters is None or request["action"] == "request_selection":
        raise ProjectPreparationError(
            "unresolved selection cannot resolve a classifier observation"
        )
    channel_rows = _sequence(request["channels"], "channels")
    channels = []
    for row in channel_rows:
        row = _closed(
            row, {"channel_id", "name", "component", "fluorophore", "biomarker"}, "channel"
        )
        try:
            channel = ChannelSpec.from_mapping(row, field="channel")
        except ROIAnalysisError as exc:
            raise ProjectPreparationError(str(exc)) from exc
        if channel.to_mapping() != row:
            raise ProjectPreparationError("request channel rows must already be canonical")
        channels.append(channel)

    # Reconstruct the request from the typed settings before trusting the
    # reply. This catches a valid-looking observation paired with other inputs.
    rebuilt = build_tissue_setup_request(
        parameters=parameters,
        channels=channels,
        native_pyramid_inventory=request["native_pyramid_inventory"],
        source=request["source"],
        expected_classifier_sha256=request["expected_classifier_sha256"],
    )
    _integer(request["channel_index"], "channel_index")
    _indices(request["permitted_native_resolution_indices"], "permitted_native_resolution_indices")
    if rebuilt != request:
        raise ProjectPreparationError(
            "tissue setup request disagrees with its scientific parameters"
        )
    observation = _closed(observation, _OBSERVATION_KEYS, "tissue setup observation")
    if observation["schema_version"] != "tissue-detector-setup-observation/v1":
        raise ProjectPreparationError("unsupported tissue setup observation schema")
    for field in ("classifier_name", "reference_channel_id", "channel_index", "channel_name"):
        if observation[field] != request[field]:
            raise ProjectPreparationError(f"observed {field} disagrees with request")
    _integer(observation["channel_index"], "channel_index")
    digest = _digest(observation["classifier_sha256"], "classifier_sha256")
    _integer(
        observation["classifier_byte_count"], "classifier_byte_count", minimum=1, maximum=MAX_TISSUE_CLASSIFIER_BYTES
    )
    if request["expected_classifier_sha256"] not in (None, digest):
        raise ProjectPreparationError("observed saved classifier hash disagrees with expected hash")
    observed_source = (
        None
        if observation["source"] is None
        else _source(observation["source"], include_path=False)
    )
    expected_source = (
        None
        if request["source"] is None
        else {key: request["source"][key] for key in ("sha256", "byte_count")}
    )
    if observed_source != expected_source:
        raise ProjectPreparationError("observed import source disagrees with request")
    if observation["classifier_type"] not in ("ThresholdPixelClassifier", "OpenCVPixelClassifier"):
        raise ProjectPreparationError(
            "observed classifier must be a supported native threshold or tissue pixel classifier"
        )
    seen = set()
    foreground = False
    for row in _sequence(observation["classification_labels"], "classification_labels"):
        row = _closed(row, {"value", "name", "ignored"}, "classification label")
        value = _integer(row["value"], "label value")
        if value in seen:
            raise ProjectPreparationError("classification labels require unique values")
        seen.add(value)
        name, ignored = row["name"], row["ignored"]
        if (
            not isinstance(ignored, bool)
            or (name is not None and (not isinstance(name, str) or not name.strip()))
            or (name is None and not ignored)
        ):
            raise ProjectPreparationError(
                "classification label requires valid name and ignored flag"
            )
        foreground |= name is not None and not ignored
    if not foreground:
        raise ProjectPreparationError("classifier requires named nonignored foreground")
    calibration = _closed(observation["input_resolution_um"], {"x", "y"}, "input_resolution_um")
    for value in calibration.values():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or value <= 0
        ):
            raise ProjectPreparationError(
                "input resolution must be positive finite x/y calibration"
            )
    levels = NativePyramidLevel.sequence_from_mappings(request["native_pyramid_inventory"])

    # Recalculate compatible levels from both reported pixel dimensions and compare QuPath’s
    # list.
    compatible = [
        item.native_resolution_index
        for item in levels
        if item.native_resolution_index in request["permitted_native_resolution_indices"]
        and abs(item.x_um_per_px - calibration["x"]) <= 1e-9
        and abs(item.y_um_per_px - calibration["y"]) <= 1e-9
    ]
    supplied = _indices(
        observation["compatible_native_resolution_indices"], "compatible_native_resolution_indices"
    )
    if not compatible or compatible != supplied:
        raise ProjectPreparationError(
            "observed calibration requires exact compatible permitted native indices"
        )

    # Add the checksum actually observed by the native model check only
    # after every requested identity and calibration relationship agrees.
    base, _ = _parameters(parameters)
    return TissueDetectionConfig.from_mapping(
        {**base.to_mapping(), "classifier_sha256": digest}, field="tissue_detection"
    )
