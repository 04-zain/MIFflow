"""

Records the accepted reference selection in processed image metadata.

The saved description includes target/source checksums, ordered measurements and
components, response-correction values and post-fit reporting weights. Validation
checks that each list covers the corresponding matrix axis and that AF positions
agree with the component kinds. The processing record then compares this description
with the image and numerical settings used for the result.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path

_AUTHORITY_KINDS = {
    "resolved_run_basis_artifact",
    "trusted_packaged_motif_profile",
    "final_run_basis_artifact",
}
_COMPONENT_KINDS = {
    "fluorophore",
    "profile_autofluorescence",
    "target_autofluorescence",
}
_BACKGROUND_DIGEST_POLICY = "mif_pipeline_typed_checksum_float64_vector_v1"


# ==============================================================================
#                                 Resolved basis
# ==============================================================================


def _text(value: object, label: str) -> str:
    """

    Checks a required identity as nonempty text without surrounding whitespace.

    Args:
        value: Identity to validate.
        label: Field description used in errors.

    Returns:
        The valid identity text.

    Raises:
        ValueError: The value is blank, non-text or padded with whitespace.
    """

    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{label} must be nonempty canonical text")
    return value


def _sha256(value: object, label: str) -> str:
    """

    Checks the format of a recorded lowercase SHA-256 checksum.

    Args:
        value: Expected 64-character hexadecimal checksum.
        label: Field description used in errors.

    Returns:
        The valid checksum.

    Raises:
        ValueError: Its type, length or characters are invalid.
    """

    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be an exact lowercase SHA-256")
    return value


def _nonnegative_integer(value: object, label: str) -> int:
    """

    Requires a nonnegative Python integer for a count or index.

    Args:
        value: Integer to inspect; booleans are rejected.
        label: Field name used in errors.

    Returns:
        The valid integer.

    Raises:
        ValueError: The value is negative or is not an integer.
    """

    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be an exact nonnegative integer")
    return value


@dataclass(frozen=True, slots=True)
class ResolvedBasisAuthorityProvenance:
    """

    Describes the reference accepted for a target image and the scales used to apply it.

    Measurement fields define the input row order, while component fields define output
    column order. Fluorophore components carry dye names and precede AF components.
    Reporting weights include the column sum and any recorded abundance rescaling.

    Attributes:
        authority_kind: Type of accepted reference source.
        authority_id: Name of the accepted record or profile.
        authority_sha256: Checksum identifying that source.
        run_id: Analysis run associated with the selection.
        target_image_sha256: Target image checksum.
        measurement_ids: Unique input measurement identities in order.
        band_names: Acquisition-band names corresponding to the measurements.
        response_transform_id: Selected measurement-response correction.
        response_transform_semantic: Operation used to apply the correction.
        response_transform_authority: Source or policy supplying the correction.
        response_measurement_ids: Measurement order associated with its multipliers.
        response_correction_multipliers: Positive float multiplier per measurement.
        component_names: Unique output component names in order.
        component_kinds: Fluorophore, profile-AF or target-AF designation per component.
        fluorophores: Dye identity for each fluorophore column and None for AF.
        biomarkers: Explicit biological markers when supplied.
        autofluorescence_components: Ordered column positions with an AF kind.
        conversion_factors: Complete positive coefficient-to-intensity weights.
        source_artifact_sha256s: Unique contributing reference-record checksums.
        ssasu_application: Reserved field that must remain None in the supported format.
    """

    authority_kind: str
    authority_id: str
    authority_sha256: str
    run_id: str
    target_image_sha256: str
    measurement_ids: tuple[str, ...]
    band_names: tuple[str, ...]
    response_transform_id: str
    response_transform_semantic: str
    response_transform_authority: str
    response_measurement_ids: tuple[str, ...]
    response_correction_multipliers: tuple[float, ...]
    component_names: tuple[str, ...]
    component_kinds: tuple[str, ...]
    fluorophores: tuple[str | None, ...]
    biomarkers: tuple[str | None, ...]
    autofluorescence_components: tuple[int, ...]
    conversion_factors: tuple[float, ...]
    source_artifact_sha256s: tuple[str, ...]
    ssasu_application: None = (
        None  # The supported processing format requires None in this reserved field.
    )

    def __post_init__(self) -> None:
        """

        Checks the reference's target identities, ordered axes and correction/reporting values.

        Require response measurement IDs to match the input measurements and component
        metadata to cover every output column. Derive AF positions from component kinds
        and compare the saved positions. Packaged MOTIF references additionally require
        dye columns followed by profile AF from one recorded source profile.

        Raises:
            ValueError: A source kind, identity, checksum, ordered list or component relationship is invalid.
        """

        if self.authority_kind not in _AUTHORITY_KINDS:
            raise ValueError("resolved basis authority kind is unsupported")
        _text(self.authority_id, "authority_id")
        _sha256(self.authority_sha256, "authority SHA-256")
        _text(self.run_id, "run_id")
        _sha256(self.target_image_sha256, "target image SHA-256")
        _unique_text_tuple(self.measurement_ids, "measurement_ids")
        _unique_text_tuple(self.band_names, "band_names")
        _unique_text_tuple(self.component_names, "component_names")
        if len(self.measurement_ids) != len(self.band_names):
            raise ValueError("resolved basis measurement and band axes differ")
        _text(self.response_transform_id, "response_transform_id")
        if self.response_transform_semantic != "multiply_normalized_measurement":
            raise ValueError("resolved basis response transform semantic is unsupported")
        _text(self.response_transform_authority, "response_transform_authority")
        _unique_text_tuple(self.response_measurement_ids, "response_measurement_ids")
        if self.response_measurement_ids != self.measurement_ids:
            raise ValueError("resolved basis response measurement axis differs")
        if (
            type(self.response_correction_multipliers) is not tuple
            or len(self.response_correction_multipliers) != len(self.measurement_ids)
            or any(
                type(value) is not float or not math.isfinite(value) or value <= 0.0
                for value in self.response_correction_multipliers
            )
        ):
            raise ValueError(
                "resolved basis response multipliers must be exact finite positive floats"
            )
        count = len(self.component_names)
        if (
            type(self.component_kinds) is not tuple
            or len(self.component_kinds) != count
            or any(item not in _COMPONENT_KINDS for item in self.component_kinds)
            or type(self.fluorophores) is not tuple
            or len(self.fluorophores) != count
            or type(self.biomarkers) is not tuple
            or len(self.biomarkers) != count
            or type(self.conversion_factors) is not tuple
            or len(self.conversion_factors) != count
        ):
            raise ValueError("resolved basis component inventories are inconsistent")
        for kind, fluorophore in zip(self.component_kinds, self.fluorophores, strict=True):
            if kind == "fluorophore":
                _text(fluorophore, "fluorophore")
            elif fluorophore is not None:
                raise ValueError("autofluorescence components cannot claim fluorophores")
        for biomarker in self.biomarkers:
            if biomarker is not None:
                _text(biomarker, "biomarker")
        for factor in self.conversion_factors:
            if type(factor) is not float or not math.isfinite(factor) or factor <= 0.0:
                raise ValueError("conversion factors must be exact finite positive floats")

        # AF positions are derived from component meaning. They cannot be
        # supplied independently to make a dye column behave as autofluorescence.
        expected_af = tuple(
            index for index, kind in enumerate(self.component_kinds) if kind != "fluorophore"
        )
        if self.autofluorescence_components != expected_af:
            raise ValueError("autofluorescence component inventory is inconsistent")
        first_af = expected_af[0] if expected_af else count
        if any(kind == "fluorophore" for kind in self.component_kinds[first_af:]):
            raise ValueError("resolved basis component order must place fluorophores before AF")
        if (
            type(self.source_artifact_sha256s) is not tuple
            or not self.source_artifact_sha256s
            or len(self.source_artifact_sha256s) != len(set(self.source_artifact_sha256s))
        ):
            raise ValueError("source artifact SHA-256 inventory is invalid")
        for value in self.source_artifact_sha256s:
            _sha256(value, "source artifact SHA-256")

        # The packaged-profile route has a specific source structure:
        # dye columns followed by profile AF, all from one recorded profile.
        if self.authority_kind == "trusted_packaged_motif_profile":
            first_af = next(
                (index for index, kind in enumerate(self.component_kinds) if kind != "fluorophore"),
                count,
            )
            if (
                first_af == 0
                or first_af == count
                or self.component_kinds[:first_af] != ("fluorophore",) * first_af
                or self.component_kinds[first_af:]
                != ("profile_autofluorescence",) * (count - first_af)
                or len(self.source_artifact_sha256s) != 1
            ):
                raise ValueError(
                    "trusted packaged MOTIF component/source inventory requires "
                    "fluorophores followed by profile AF from one profile artifact"
                )
        if self.ssasu_application is not None:
            raise ValueError(
                "Retired background-estimation provenance is not supported; use reference-based unmixing"
            )


def _unique_text_tuple(value: object, label: str) -> tuple[str, ...]:
    """

    Checks an ordered identity list for nonempty, unpadded and unique text values.

    Args:
        value: Tuple of identities to inspect.
        label: List name used in errors.

    Returns:
        The validated identity tuple.

    Raises:
        ValueError: The tuple is empty, an entry is invalid or an identity repeats.
    """

    if (
        type(value) is not tuple
        or not value
        or any(type(item) is not str or not item or item.strip() != item for item in value)
        or len(value) != len(set(value))
    ):
        raise ValueError(f"{label} must be a nonempty unique canonical tuple")
    return value


def validate_resolved_basis_authority_provenance(
    authority: ResolvedBasisAuthorityProvenance,
) -> None:
    """

    Reconstructs a reference description to repeat its field checks before use.

    Require the expected record type and rebuild it from all current fields. The
    result must equal the supplied record, confirming that its values still satisfy
    construction checks before they enter a processing record.

    Args:
        authority: ResolvedBasisAuthorityProvenance to recheck.

    Raises:
        TypeError: The record has an unsupported type.
        ValueError: Fields fail reconstruction or produce a different value.
    """

    if type(authority) is not ResolvedBasisAuthorityProvenance:
        raise TypeError("resolved basis authority must be exact provenance")
    replayed = ResolvedBasisAuthorityProvenance(
        authority_kind=authority.authority_kind,
        authority_id=authority.authority_id,
        authority_sha256=authority.authority_sha256,
        run_id=authority.run_id,
        target_image_sha256=authority.target_image_sha256,
        measurement_ids=authority.measurement_ids,
        band_names=authority.band_names,
        response_transform_id=authority.response_transform_id,
        response_transform_semantic=authority.response_transform_semantic,
        response_transform_authority=authority.response_transform_authority,
        response_measurement_ids=authority.response_measurement_ids,
        response_correction_multipliers=authority.response_correction_multipliers,
        component_names=authority.component_names,
        component_kinds=authority.component_kinds,
        fluorophores=authority.fluorophores,
        biomarkers=authority.biomarkers,
        autofluorescence_components=authority.autofluorescence_components,
        conversion_factors=authority.conversion_factors,
        source_artifact_sha256s=authority.source_artifact_sha256s,
        ssasu_application=authority.ssasu_application,
    )
    if replayed != authority:
        raise ValueError("resolved basis authority failed recursive replay")


__all__ = [
    "ResolvedBasisAuthorityProvenance",
    "validate_resolved_basis_authority_provenance",
]
