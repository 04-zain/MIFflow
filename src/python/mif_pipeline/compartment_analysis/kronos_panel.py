"""

Associates the selected image channels with the KRONOS marker list and prepares their pixel intensities.

We select the image markers and supply one intensity divisor per channel.
The model's marker CSV supplies known marker names and their statistics;
explicit additional rows supply novel identities and measured reference values. In each image patch, channel c is prepared as:

    normalized[c] = (intensity[c] / divisor[c] - mean[c]) / std[c]

Channel order and these values are saved together. The same input calculation
is therefore used when extracting training features and applying the accepted
classifier. The patch reader supplies the recorded physical scale and the
recorded square input arrays before normalization begins.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import numpy as np
from .kronos_marker_preparation import (
    KronosAdditionalMarker, KronosMarkerTextModel, resolve_marker_selection,
)

# ==============================================================================
#                                  Kronos panel
# ==============================================================================


@dataclass(frozen=True, slots=True)
class KronosPanel:
    """

    Retains the ordered markers and numerical preparation used by the encoder.

    The chosen image-marker names identify the selected channels. Their model
    names and statistics come from the supplied KRONOS CSV or explicit additional
    rows with measured reference values. We retain the selected order and complete
    metadata so fitting and application use the same preparation.

    Attributes:
        marker_names: Explicit image-marker names in selected channel order.
        model_marker_names: Corresponding names recognized by the pretrained model.
        intensity_divisors: Positive per-channel values used to convert image
            intensities to the scale preceding model normalization.
        pixel_size_um: X/Y physical pixel sizes used when sampling the input patches.
        means: Per-marker model means subtracted after intensity division.
        standard_deviations: Positive model standard deviations used for scaling.
        marker_csv_sha256: Checksum of the CSV supplying names and statistics.
        patch_size: Saved square image side in samples: 32, 64, 128 or 256.
        additional_markers: Novel selected identities with explicit descriptions,
            measured statistics and their reference source.
        marker_text_model: Pinned local BioLinkBERT identity for those rows, or absent.
        nuclear_channel: Optional selected channel position explicitly mapped
            to the DAPI entry in that CSV.
    """

    marker_names: tuple[str, ...]
    model_marker_names: tuple[str, ...]
    intensity_divisors: tuple[float, ...]
    pixel_size_um: tuple[float, float]
    means: tuple[float, ...]
    standard_deviations: tuple[float, ...]
    marker_csv_sha256: str
    patch_size: int
    additional_markers: tuple[KronosAdditionalMarker, ...]
    marker_text_model: KronosMarkerTextModel | None
    nuclear_channel: int | None = None

    def __post_init__(self):
        """

        Checks that channel order, marker identities and normalization values form one input panel.

        We require one to 32 selected markers and the same number of model names,
        divisors, means and standard deviations. Model marker names must be distinct.
        Divisors, standard deviations and X/Y pixel sizes must be finite and positive;
        means must be finite. The optional nuclear-channel index must identify one
        selected channel.

        Raises:
            ValueError: Marker/parameter lengths, tuple representations, numerical
                values, vocabulary checksum or nuclear-channel position are invalid.
        """

        if type(self.patch_size) is not int or self.patch_size not in (32, 64, 128, 256):
            raise ValueError("KRONOS image size must be an integer: 32, 64, 128 or 256")
        if type(self.additional_markers) is not tuple or any(
            type(row) is not KronosAdditionalMarker for row in self.additional_markers
        ):
            raise ValueError("Additional markers require ordered checked metadata rows")
        if bool(self.additional_markers) != (self.marker_text_model is not None):
            raise ValueError("Additional markers require their selected BioLinkBERT snapshot; known-only panels need no helper")
        if self.marker_text_model is not None and type(self.marker_text_model) is not KronosMarkerTextModel:
            raise ValueError("Additional-marker helper selection must be a checked model identity")
        count = len(self.marker_names)
        vectors = (
            self.marker_names,
            self.model_marker_names,
            self.intensity_divisors,
            self.pixel_size_um,
            self.means,
            self.standard_deviations,
        )
        if any(type(v) is not tuple for v in vectors):
            raise ValueError("KRONOS panel values must be immutable tuples")
        if not 1 <= count <= 32 or any(
            type(v) is not str or not v.strip() for v in self.marker_names
        ):
            raise ValueError("KRONOS requires 1–32 explicitly named marker channels")
        if any(
            len(v) != count
            for v in (
                self.model_marker_names,
                self.intensity_divisors,
                self.means,
                self.standard_deviations,
            )
        ):
            raise ValueError("KRONOS panel vectors must match the marker count")
        if len(set(self.model_marker_names)) != count:
            raise ValueError("duplicate model marker identity")
        if len(self.pixel_size_um) != 2:
            raise ValueError("physical pixel size requires independent X and Y values")
        for values in (self.intensity_divisors, self.standard_deviations, self.pixel_size_um):
            if any(
                isinstance(v, bool) or not math.isfinite(float(v)) or float(v) <= 0 for v in values
            ):
                raise ValueError(
                    "divisors, standard deviations and pixel sizes must be finite positive values"
                )
        if any(not math.isfinite(float(v)) for v in self.means):
            raise ValueError("marker means must be finite")
        if self.nuclear_channel is not None and (
            type(self.nuclear_channel) is not int or not 0 <= self.nuclear_channel < count
        ):
            raise ValueError("nuclear channel must be an explicit channel index or absent")
        for name in ("intensity_divisors", "pixel_size_um", "means", "standard_deviations"):
            object.__setattr__(self, name, tuple(float(value) for value in getattr(self, name)))
        if len(self.marker_csv_sha256) != 64 or any(
            c not in "0123456789abcdef" for c in self.marker_csv_sha256
        ):
            raise ValueError("marker vocabulary checksum is invalid")

    @classmethod
    def from_csv(
        cls,
        path: Path,
        *,
        marker_names: tuple[str, ...],
        intensity_divisors: tuple[float, ...],
        pixel_size_um: tuple[float, float],
        patch_size: int,
        additional_markers: tuple[KronosAdditionalMarker, ...],
        marker_text_model: KronosMarkerTextModel | None,
        nuclear_channel: int | None = None,
    ):
        """

        Resolves selected image markers to base or explicit additional normalization.

        We read marker_name, mean and std from each CSV row and index them by the
        normalized marker key. Selected channels are then matched in their input
        order. An explicitly selected nuclear channel uses the DAPI row. The complete
        CSV bytes are hashed once to identify the base statistics; explicit
        additional metadata and helper identity enter the complete panel mapping.

        Args:
            path: Marker CSV supplied with the selected pretrained KRONOS package.
            marker_names: Image-marker names in the selected input-channel order.
            intensity_divisors: Positive per-channel divisors supplied for the images.
            pixel_size_um: Physical X/Y sampling sizes of the encoder patches, in µm/pixel.
            patch_size: Saved square image side in samples.
            additional_markers: Ordered metadata for every unknown selected marker.
            marker_text_model: Inspected helper identity when additional rows are supplied.
            nuclear_channel: Optional zero-based selected-channel position to identify as DAPI.

        Returns:
            KronosPanel with the resolved model names, means, standard deviations
            and input-channel settings.

        Raises:
            ValueError: The file exceeds 4 MiB, a name is missing or ambiguous,
                selected markers are absent, or normalization values are invalid.
            OSError: The marker CSV cannot be read.
        """

        return cls.from_csv_bytes(Path(path).read_bytes(), marker_names=marker_names,
            intensity_divisors=intensity_divisors, pixel_size_um=pixel_size_um,
            patch_size=patch_size, additional_markers=additional_markers,
            marker_text_model=marker_text_model, nuclear_channel=nuclear_channel)

    @classmethod
    def from_csv_bytes(cls, raw: bytes, *, marker_names: tuple[str, ...],
                       intensity_divisors: tuple[float, ...], pixel_size_um: tuple[float, float],
                       patch_size: int, additional_markers: tuple[KronosAdditionalMarker, ...],
                       marker_text_model: KronosMarkerTextModel | None, nuclear_channel: int | None = None):
        """Resolve the same marker statistics received in a compute-host review.

        The cluster sends these bounded CSV bytes with its selected package
        checksum; execution rechecks the package on that host before use.
        """
        selected = resolve_marker_selection(raw, marker_names, nuclear_channel, additional_markers)
        return cls(
            tuple(marker_names),
            tuple(v[0] for v in selected),
            tuple(intensity_divisors),
            tuple(pixel_size_um),
            tuple(v[1] for v in selected),
            tuple(v[2] for v in selected),
            hashlib.sha256(raw).hexdigest(),
            patch_size,
            additional_markers,
            marker_text_model,
            nuclear_channel,
        )

    def to_mapping(self):
        """

        Collects the complete panel values for the encoder request and saved classifier.

        Returns:
            Current kronos-feature-panel/v1 dictionary with ordered image/model
            marker names, divisors, means, standard deviations, physical sampling
            scale, vocabulary checksum and the selected input size.
        """

        return {
            "schema": "kronos-feature-panel/v1",
            "patch_size": self.patch_size,
            "additional_markers": [row.to_mapping() for row in self.additional_markers],
            "marker_text_model": None if self.marker_text_model is None else self.marker_text_model.to_mapping(),
            "marker_names": list(self.marker_names),
            "model_marker_names": list(self.model_marker_names),
            "intensity_divisors": list(self.intensity_divisors),
            "pixel_size_um": list(self.pixel_size_um),
            "means": list(self.means),
            "standard_deviations": list(self.standard_deviations),
            "marker_csv_sha256": self.marker_csv_sha256,
            "nuclear_channel": self.nuclear_channel,
        }

    @property
    def sha256(self):
        """

        Calculates a checksum covering every saved panel setting and its channel order.

        We serialize the panel dictionary with sorted keys and fixed JSON separators.
        Changing a selected marker, divisor, statistic or physical scale therefore
        changes the panel checksum used by the encoder and classifier checks.

        Returns:
            Lowercase SHA-256 of the encoded panel values.
        """

        raw = json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def normalize(self, patches):
        """

        Prepares the selected channel intensities for one batch of KRONOS image patches.

        We copy the input into contiguous float32 storage, then apply each channel's
        divisor, model mean and model standard deviation:

            output[:,c] = (patches[:,c] / intensity_divisors[c] - means[c])
                          / standard_deviations[c]

        For a pixel intensity of 1000, divisor 100, mean 2 and standard deviation 4,
        the normalized value is (1000/100 - 2)/4 = 2. The same three values are applied
        to every pixel of that channel and every patch in this request. Adding
        singleton axes around the channel vector lets NumPy perform those operations
        across the batch and spatial dimensions without constructing factor images.

        Args:
            patches: Real finite array of shape (N, channels, patch_size, patch_size), with one
                to sixteen patches and channels in this panel's order.

        Returns:
            Contiguous float32 array with normalized intensities and the same shape.
            The input image array is not modified.

        Raises:
            ValueError: Input dimensions or values are invalid, or conversion and
                normalization produce nonfinite float32 intensities.
        """

        values = np.asarray(patches)
        if (
            values.ndim != 4
            or values.shape[1:] != (len(self.marker_names), self.patch_size, self.patch_size)
            or not 1 <= values.shape[0] <= 16
        ):
            raise ValueError(
                f"KRONOS requires 1–16 explicit N×C×{self.patch_size}×{self.patch_size} patches; no implicit resampling"
            )
        if (
            not np.issubdtype(values.dtype, np.number)
            or np.iscomplexobj(values)
            or not np.all(np.isfinite(values))
        ):
            raise ValueError("KRONOS input pixels must be real and finite")
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            out = np.array(values, dtype=np.float32, order="C", copy=True)
            # Shape (1, channels, 1, 1) applies each selected scalar across
            # the batch, rows and columns while retaining channel order.
            out /= np.asarray(self.intensity_divisors, dtype=np.float32)[None, :, None, None]
            out -= np.asarray(self.means, dtype=np.float32)[None, :, None, None]
            out /= np.asarray(self.standard_deviations, dtype=np.float32)[None, :, None, None]
        if not np.all(np.isfinite(out)):
            raise ValueError("prepared KRONOS pixels must remain finite in float32")
        return out
