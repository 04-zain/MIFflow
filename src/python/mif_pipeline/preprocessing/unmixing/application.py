"""

Prepares a reviewed spectral reference for application to its selected target image.

The accepted reference identifies the target image, measurement order, component
spectra and response correction. It may also specify a rescaling of fitted component
amounts. This module combines the spectral column sum and that rescaling into one
reporting weight per component, which the regional engine applies after fitting.

The numerical values are retained as tuples with the accepted source record.
Before processing, the same record can be reconstructed and compared with its saved
values to check that the target/reference selection remains consistent.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
import math

import numpy as np
from numpy.typing import NDArray

from .compatibility import BasisSourceResolution
from .reference_models import (
    BasisSourceLineage,
    QuantitativeTransformSignature,
    ResolvedBasisColumn,
    ResolvedRunBasis,
    ReviewRecord,
)
from .normalization import ResponseCorrectionTransform
from .target_af import (
    ResolvedRunBasisArtifact,
    resolved_run_basis_handoff,
)

_APPLICATION_TOKEN = object()


# ==============================================================================
#                                  Application
# ==============================================================================


@dataclass(frozen=True, slots=True)
class ResolvedBasisTargetBinding:
    """

    Identifies the image and ordered measurements for which a reference will be used.

    Attributes:
        target_image_sha256: Checksum of the target image contents.
        canonical_image_id: Image identifier recorded during import.
        measurement_ids: Ordered measurement identities used for pixel reading.
        band_names: Acquisition-band names corresponding to those measurements.
        width_px: Full-resolution image width in pixels.
        height_px: Full-resolution image height in pixels.
    """

    target_image_sha256: str
    canonical_image_id: str
    measurement_ids: tuple[str, ...]
    band_names: tuple[str, ...]
    width_px: int
    height_px: int

    def __post_init__(self) -> None:
        """

        Checks the target checksum, measurement coverage and positive image dimensions.

        Measurement IDs must be unique, with one nonempty band name for each. Image dimensions
        must be positive integers so they can be compared with the regional reader's geometry.

        Raises:
            ValueError: An identity, checksum, band list or dimension is invalid.
        """

        _sha256(self.target_image_sha256, "target_image_sha256")
        if type(self.canonical_image_id) is not str or not self.canonical_image_id:
            raise ValueError("canonical_image_id must be nonempty")
        if (
            type(self.measurement_ids) is not tuple
            or not self.measurement_ids
            or any(type(item) is not str or not item for item in self.measurement_ids)
            or len(self.measurement_ids) != len(set(self.measurement_ids))
        ):
            raise ValueError("measurement_ids must be a nonempty unique tuple")
        if (
            type(self.band_names) is not tuple
            or len(self.band_names) != len(self.measurement_ids)
            or any(type(item) is not str or not item for item in self.band_names)
        ):
            raise ValueError("band_names must align with measurement_ids")
        if (
            type(self.width_px) is not int
            or type(self.height_px) is not int
            or self.width_px <= 0
            or self.height_px <= 0
        ):
            raise ValueError("target dimensions must be positive exact integers")


@dataclass(frozen=True, slots=True)
class ResolvedBasisApplication:
    """

    Keeps the accepted target/reference choices together for the regional unmixing engine.

    Factories construct this record from a reviewed reference or packaged-library
    resolution. The reference matrix is stored by measurement row and component column.
    Reporting weights include both spectral column sums and any recorded abundance
    rescaling. Array properties expose these values as read-only float64 arrays.

    Attributes:
        authority_kind: Type of accepted reference record supplying the calculation.
        authority_id: Name of that record or packaged profile.
        authority_sha256: Checksum identifying the accepted reference contents.
        run_id: Analysis run associated with the reference selection.
        target_image_sha256: Target image checksum associated with the acceptance.
        measurement_ids: Ordered matrix-row identities.
        band_names: Acquisition-band names for those rows.
        component_names: Fluorescence-source names in matrix-column order.
        component_kinds: Fluorophore or AF kind for each component.
        fluorophores: Explicit dye names, with None for AF components.
        biomarkers: Optional biological marker assignments accepted with the final reference.
        autofluorescence_components: Column positions representing AF.
        source_artifact_sha256s: Checksums of contributing reference records.
        response_transform_id: Selected measurement-response correction.
        response_transform_semantic: Operation used to apply that correction.
        response_transform_authority: Source or policy supplying the correction.
        response_measurement_ids: Measurement order of its multiplier vector.
        response_correction_multipliers: Positive response multipliers applied before fitting.
        _matrix_values: Spectral values stored as tuples of measurement rows.
        _conversion_values: Complete post-fit reporting factor for each component.
        _authority: Accepted source object retained for later reconstruction checks.
        _token: Internal constructor token supplied by the accepted-reference factories.
    """

    authority_kind: str
    authority_id: str
    authority_sha256: str
    run_id: str
    target_image_sha256: str
    measurement_ids: tuple[str, ...]
    band_names: tuple[str, ...]
    component_names: tuple[str, ...]
    component_kinds: tuple[str, ...]
    fluorophores: tuple[str | None, ...]
    biomarkers: tuple[str | None, ...]
    autofluorescence_components: tuple[int, ...]
    source_artifact_sha256s: tuple[str, ...]
    response_transform_id: str
    response_transform_semantic: str
    response_transform_authority: str
    response_measurement_ids: tuple[str, ...]
    response_correction_multipliers: tuple[float, ...]
    _matrix_values: tuple[tuple[float, ...], ...] = field(repr=False)
    _conversion_values: tuple[float, ...] = field(repr=False)
    _authority: object = field(repr=False, compare=False)
    _token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        """

        Checks that the constructed reference has matching measurement, component and factor dimensions.

        Require the accepted-source constructor token, valid source/target identities and
        one reporting weight per component. The response declaration must cover the same
        ordered measurements as the matrix.

        Raises:
            TypeError: Construction did not use the accepted-reference factory.
            ValueError: Identities, dimensions or response/reporting factors disagree.
        """

        if self._token is not _APPLICATION_TOKEN:
            raise TypeError(
                "ResolvedBasisApplication requires an accepted typed authority constructor"
            )
        if not self.authority_kind or not self.authority_id:
            raise ValueError("basis application authority identity must be nonempty")
        _sha256(self.authority_sha256, "authority_sha256")
        _sha256(self.target_image_sha256, "target_image_sha256")
        if not self.run_id:
            raise ValueError("basis application run_id must be nonempty")
        count = len(self.component_names)
        if (
            not self.measurement_ids
            or len(self.measurement_ids) != len(self.band_names)
            or count == 0
            or len(self.component_kinds) != count
            or len(self.fluorophores) != count
            or len(self.biomarkers) != count
            or len(self._conversion_values) != count
        ):
            raise ValueError("basis application inventories have incompatible lengths")
        if len(self._matrix_values) != len(self.measurement_ids) or any(
            len(row) != count for row in self._matrix_values
        ):
            raise ValueError("basis application matrix shape conflicts with its inventories")
        response = self.response_transform
        if response.measurement_ids != self.measurement_ids:
            raise ValueError(
                "basis application response transform axis conflicts with measurements"
            )
        if any(not math.isfinite(value) or value <= 0.0 for value in self._conversion_values):
            raise ValueError("basis application conversion factors must be finite and positive")

    @classmethod
    def from_motif_resolution(
        cls,
        resolution: BasisSourceResolution,
    ) -> "ResolvedBasisApplication":
        """

        Prepares a target-specific runtime reference from an accepted packaged MOTIF resolution.

        Require the packaged-MOTIF route, a resolved matrix and recorded target/checksum
        information. All selected columns must refer to one packaged profile. The target's
        quantitative settings supply the response correction used before fitting.

        Args:
            resolution: Completed BasisSourceResolution from the packaged-reference workflow.

        Returns:
            A ResolvedBasisApplication ready for target and normalization checks.

        Raises:
            TypeError, ValueError: The resolution type, route, readiness or source profile is invalid.
        """

        if type(resolution) is not BasisSourceResolution:
            raise TypeError("resolution must be exact BasisSourceResolution")
        if resolution.authority_path != "trusted_packaged_motif_profile":
            raise ValueError("basis resolution is not trusted packaged MOTIF authority")
        if resolution.resolved_basis is None or resolution.resolution_evidence_sha256 is None:
            raise ValueError("trusted MOTIF resolution is not ready")
        basis = resolution.resolved_basis
        source_ids = tuple(dict.fromkeys(item.source.artifact_id for item in basis.columns))
        if len(source_ids) != 1:
            raise ValueError("trusted MOTIF columns do not share one profile identity")
        return cls._from_basis(
            basis=basis,
            authority_kind="trusted_packaged_motif_profile",
            authority_id=source_ids[0],
            authority_sha256=resolution.resolution_evidence_sha256,
            target_image_sha256=_required_sha256(
                resolution.target_image_sha256,
                "MOTIF target_image_sha256",
            ),
            source_artifact_sha256s=_column_source_hashes(basis.columns),
            quantitative_signature=resolution.target_quantitative_signature,
            authority=resolution,
        )

    @classmethod
    def from_task7_artifact(
        cls,
        artifact: ResolvedRunBasisArtifact,
    ) -> "ResolvedBasisApplication":
        """

        Prepares the numerical reference from an accepted resolved-basis record.

        Validate the record through its handoff function, retain its target and source
        checksums, and use the target-AF model's quantitative settings for response correction.

        Args:
            artifact: Accepted ResolvedRunBasisArtifact.

        Returns:
            The matrix, component descriptions and reporting factors for that target.

        Raises:
            TypeError, ValueError: The record or its accepted-basis handoff is invalid.
        """

        if type(artifact) is not ResolvedRunBasisArtifact:
            raise TypeError("artifact must be exact ResolvedRunBasisArtifact")
        basis = resolved_run_basis_handoff(artifact)
        return cls._from_basis(
            basis=basis,
            authority_kind="resolved_run_basis_artifact",
            authority_id=artifact.artifact_id,
            authority_sha256=artifact.content_sha256,
            target_image_sha256=artifact.target_image_sha256,
            source_artifact_sha256s=artifact.source_artifact_sha256s,
            quantitative_signature=(
                artifact.candidate.target_af_artifact.model.quantitative_signature
            ),
            authority=artifact,
        )

    @classmethod
    def _from_service_final_authority(
        cls,
        artifact,
        handoff,
        *,
        service_authority_proof: object,
    ) -> "ResolvedBasisApplication":
        """

        Prepares the reference service's final basis after checking its associated handoff.

        The final record and handoff must match, and the reference service must supply
        its internal proof object. Retain that relationship so the same accepted choice
        can be checked again before image processing.

        Args:
            artifact: Final accepted reference record.
            handoff: Runtime handoff associated with that record.
            service_authority_proof: Internal proof supplied by the reference service.

        Returns:
            A ResolvedBasisApplication linked to the checked final reference.

        Raises:
            TypeError, ValueError: The proof, record type or handoff comparison is invalid.
        """

        from . import reference_service
        from .final_basis import (
            FinalRunBasisArtifact,
            FinalRunBasisRuntimeHandoff,
        )

        if service_authority_proof is not reference_service._FINAL_APPLICATION_SERVICE_AUTHORITY:
            raise TypeError("final application requires service authority proof")
        if type(artifact) is not FinalRunBasisArtifact:
            raise TypeError("artifact must be exact FinalRunBasisArtifact")
        if type(handoff) is not FinalRunBasisRuntimeHandoff:
            raise TypeError("handoff must be exact FinalRunBasisRuntimeHandoff")
        handoff.validate_artifact(artifact)
        proposal = artifact.proposal
        return cls._from_basis(
            basis=artifact.basis,
            authority_kind="final_run_basis_artifact",
            authority_id=artifact.artifact_id,
            authority_sha256=artifact.content_sha256,
            target_image_sha256=proposal.target_image_sha256,
            source_artifact_sha256s=proposal.source_artifact_sha256s,
            quantitative_signature=proposal.quantitative_signature,
            authority=(artifact, handoff, service_authority_proof),
            biomarkers=artifact.biomarkers,
        )

    @classmethod
    def _from_basis(
        cls,
        *,
        basis: ResolvedRunBasis,
        authority_kind: str,
        authority_id: str,
        authority_sha256: str,
        target_image_sha256: str,
        source_artifact_sha256s: tuple[str, ...],
        quantitative_signature: QuantitativeTransformSignature,
        authority: object,
        biomarkers: tuple[str | None, ...] | None = None,
    ) -> "ResolvedBasisApplication":
        """

        Arranges accepted component spectra into a matrix and calculates their complete reporting weights.

        Each component contains a vector across the measurements. Read the corresponding
        position from every vector to form matrix rows. For component j, its reporting
        weight is sum(column[j].values) × abundance_rescaling_factor[j]. Combining those
        terms here lets the regional engine apply the full conversion once after fitting.

        Args:
            basis: Accepted ordered measurements and component vectors.
            authority_kind: Reference-source kind chosen by the factory.
            authority_id: Accepted record or profile identifier.
            authority_sha256: Checksum identifying that accepted source.
            target_image_sha256: Target image for which the reference was reviewed.
            source_artifact_sha256s: Ordered checksums of contributing reference records.
            quantitative_signature: Target measurement order and response-correction settings.
            authority: Source object retained for later reconstruction checks.

        Returns:
            The runtime reference with matrix values, correction settings and reporting weights.

        Raises:
            TypeError, ValueError: Measurement order, component order or constructed values differ.
        """

        if type(quantitative_signature) is not QuantitativeTransformSignature:
            raise TypeError("basis application requires exact target quantitative signature")
        if quantitative_signature.measurement_ids != basis.axis.measurement_ids:
            raise ValueError("target quantitative transform axis differs from resolved basis")
        response_multipliers = tuple(
            float(item.response_correction_multiplier)
            for item in quantitative_signature.measurements
        )

        # Component vectors are stored as columns in the reviewed reference.
        # Build immutable measurement rows for the runtime matrix interface.
        matrix_values = tuple(
            tuple(float(column.values[row]) for column in basis.columns)
            for row in range(len(basis.axis.measurements))
        )

        # Combine both reporting-scale terms here. The engine applies this
        # resulting vector once; multiplying a separate column sum would double-scale.
        conversion_values = tuple(
            math.fsum(column.values) * float(column.abundance_rescaling_factor)
            for column in basis.columns
        )
        kinds = tuple(item.component_kind for item in basis.columns)
        first_af = next(
            (index for index, kind in enumerate(kinds) if kind != "fluorophore"),
            len(kinds),
        )
        if any(kind == "fluorophore" for kind in kinds[first_af:]):
            raise ValueError("resolved basis order must place fluorophores before AF columns")
        source_hashes = tuple(source_artifact_sha256s)
        if not source_hashes or len(source_hashes) != len(set(source_hashes)):
            raise ValueError("source artifact hashes must be nonempty and unique")
        for value in source_hashes:
            _sha256(value, "source_artifact_sha256")
        return cls(
            authority_kind=authority_kind,
            authority_id=authority_id,
            authority_sha256=authority_sha256,
            run_id=basis.run_id,
            target_image_sha256=target_image_sha256,
            measurement_ids=basis.axis.measurement_ids,
            band_names=basis.axis.parent_band_ids,
            component_names=basis.component_order,
            component_kinds=kinds,
            fluorophores=tuple(item.fluorophore_identity for item in basis.columns),
            biomarkers=(None,) * len(basis.columns) if biomarkers is None else biomarkers,
            autofluorescence_components=tuple(
                index
                for index, item in enumerate(basis.columns)
                if item.component_kind != "fluorophore"
            ),
            source_artifact_sha256s=source_hashes,
            response_transform_id=quantitative_signature.response_transform_id,
            response_transform_semantic=(quantitative_signature.response_transform_semantic),
            response_transform_authority=(quantitative_signature.response_transform_authority),
            response_measurement_ids=quantitative_signature.measurement_ids,
            response_correction_multipliers=response_multipliers,
            _matrix_values=matrix_values,
            _conversion_values=conversion_values,
            _authority=authority,
            _token=_APPLICATION_TOKEN,
        )

    @property
    def identifier(self) -> str:
        """

        Supplies the accepted reference name through the engine's library interface.

        Returns:
            authority_id.
        """

        return self.authority_id

    @property
    def sha256(self) -> str:
        """

        Supplies the accepted reference checksum through the engine's library interface.

        Returns:
            authority_sha256.
        """

        return self.authority_sha256

    @property
    def band_count(self) -> int:
        """

        Returns the number of measurements represented by matrix rows.

        Returns:
            The length of measurement_ids.
        """

        return len(self.measurement_ids)

    @property
    def component_count(self) -> int:
        """

        Returns the number of fluorescence components represented by matrix columns.

        Returns:
            The length of component_names.
        """

        return len(self.component_names)

    @property
    def matrix(self) -> NDArray[np.float64]:
        """

        Creates the numerical reference array used when preparing the spectral solver.

        Returns:
            A read-only float64 array with shape (measurements, components).
        """

        return _readonly_array(self._matrix_values)

    @property
    def conversion_factors(self) -> NDArray[np.float64]:
        """

        Creates the complete coefficient-to-intensity reporting vector.

        Returns:
            Read-only float64 weights containing spectral column sums multiplied by
            their accepted abundance-rescaling factors.
        """

        return _readonly_array(self._conversion_values)

    @property
    def column_sums(self) -> NDArray[np.float64]:
        """

        Supplies complete reporting weights through the regional engine's column_sums interface.

        Returns:
            The conversion_factors vector, including any accepted abundance rescaling.
        """

        return self.conversion_factors

    @property
    def response_transform(self) -> ResponseCorrectionTransform:
        """

        Constructs the measurement-response correction from the accepted target settings.

        Returns:
            A checked ResponseCorrectionTransform with the recorded measurement order
            and per-band multipliers.
        """

        return ResponseCorrectionTransform(
            transform_id=self.response_transform_id,
            semantic=self.response_transform_semantic,
            authority=self.response_transform_authority,
            measurement_ids=self.response_measurement_ids,
            multipliers=self.response_correction_multipliers,
        )

    def validate_target(
        self,
        *,
        target_image_sha256: str,
        measurement_ids: tuple[str, ...],
        band_names: tuple[str, ...],
    ) -> None:
        """

        Compares the target image and measurement order with those used to accept the reference.

        Args:
            target_image_sha256: Checksum obtained from the selected target image.
            measurement_ids: Measurements in the proposed pixel-reading order.
            band_names: Acquisition-band names associated with those measurements.

        Raises:
            ValueError: The target checksum or either ordered list differs.
        """

        if target_image_sha256 != self.target_image_sha256:
            raise ValueError("basis application target image SHA-256 differs")
        if measurement_ids != self.measurement_ids:
            raise ValueError("basis application measurement axis differs")
        if band_names != self.band_names:
            raise ValueError("basis application measurement-band axis differs")

    def validate_target_binding(self, binding: ResolvedBasisTargetBinding) -> None:
        """

        Checks the target record's type, image checksum and ordered measurements against this reference.

        Args:
            binding: ResolvedBasisTargetBinding prepared from the selected image.

        Raises:
            TypeError, ValueError: The target record type or image/measurement identities differ.
        """

        if type(binding) is not ResolvedBasisTargetBinding:
            raise TypeError("binding must be exact ResolvedBasisTargetBinding")
        self.validate_target(
            target_image_sha256=binding.target_image_sha256,
            measurement_ids=binding.measurement_ids,
            band_names=binding.band_names,
        )

    def revalidate_authority(self) -> None:
        """

        Reconstructs the runtime reference from its retained accepted source and compares the results.

        Use the same factory that originally prepared the reference, repeating its source
        and handoff checks. Then compare target identity, measurement/component order,
        response values, matrix tuples and reporting weights. A changed source relationship
        therefore cannot silently alter the numerical reference being applied.

        Raises:
            TypeError, ValueError: The accepted source fails reconstruction or produces
                different runtime values.
        """

        if self.authority_kind == "resolved_run_basis_artifact":
            artifact = self._authority
            if type(artifact) is not ResolvedRunBasisArtifact:
                raise TypeError("retained Task-7 authority has the wrong type")
            rebuilt = ResolvedBasisApplication.from_task7_artifact(artifact)
        elif self.authority_kind == "trusted_packaged_motif_profile":
            resolution = self._authority
            if type(resolution) is not BasisSourceResolution:
                raise TypeError("retained MOTIF authority has the wrong type")
            rebuilt = ResolvedBasisApplication.from_motif_resolution(resolution)
        elif self.authority_kind == "final_run_basis_artifact":
            from .final_basis import (
                FinalRunBasisArtifact,
                FinalRunBasisRuntimeHandoff,
            )

            authority = self._authority
            if (
                type(authority) is not tuple
                or len(authority) != 3
                or type(authority[0]) is not FinalRunBasisArtifact
                or type(authority[1]) is not FinalRunBasisRuntimeHandoff
            ):
                raise TypeError("retained final basis authority has the wrong type")
            rebuilt = ResolvedBasisApplication._from_service_final_authority(
                authority[0],
                authority[1],
                service_authority_proof=authority[2],
            )
        else:
            raise ValueError("basis application authority kind is unsupported")

        # Compare reconstructed identities, component order, spectral values and reporting
        # factors.
        if _runtime_snapshot(rebuilt) != _runtime_snapshot(self):
            raise ValueError("retained basis authority differs from application")


def _runtime_snapshot(application: ResolvedBasisApplication) -> tuple[object, ...]:
    """

    Collects the ordered reference fields used when comparing a reconstructed application.

    Args:
        application: Runtime reference to describe.

    Returns:
        A tuple of identities, measurement/component fields, response values,
        spectral matrix values and reporting weights.
    """

    return tuple(
        getattr(application, name)
        for name in (
            "authority_kind",
            "authority_id",
            "authority_sha256",
            "run_id",
            "target_image_sha256",
            "measurement_ids",
            "band_names",
            "component_names",
            "component_kinds",
            "fluorophores",
            "biomarkers",
            "autofluorescence_components",
            "source_artifact_sha256s",
            "response_transform_id",
            "response_transform_semantic",
            "response_transform_authority",
            "response_measurement_ids",
            "response_correction_multipliers",
            "_matrix_values",
            "_conversion_values",
        )
    )


def _column_source_hashes(
    columns: tuple[ResolvedBasisColumn, ...],
) -> tuple[str, ...]:
    """

    Collects the source-record checksums contributing to the component columns.

    Args:
        columns: Accepted component columns and their source descriptions.

    Returns:
        Unique source checksums in first-occurrence order.
    """

    return tuple(dict.fromkeys(item.source.content_sha256 for item in columns))


def _readonly_array(values) -> NDArray[np.float64]:
    """

    Converts stored numerical tuples into a read-only contiguous float64 array.

    Args:
        values: Spectral matrix or reporting-vector values.

    Returns:
        The numerical array in the corresponding shape.
    """

    result = np.array(values, dtype=np.float64, copy=True, order="C")
    result.setflags(write=False)
    return result


def _required_sha256(value: str | None, label: str) -> str:
    """

    Requires a recorded checksum and checks its lowercase hexadecimal format.

    Args:
        value: Source checksum, which must be present.
        label: Field description used in errors.

    Returns:
        The valid checksum.

    Raises:
        ValueError: The checksum is missing or malformed.
    """

    if value is None:
        raise ValueError(f"{label} is required")
    _sha256(value, label)
    return value


def _sha256(value: str, label: str) -> None:
    """

    Checks that a recorded SHA-256 value contains 64 lowercase hexadecimal characters.

    Args:
        value: Checksum text to inspect.
        label: Field description used in errors.

    Raises:
        ValueError: The checksum format is invalid.
    """

    if (
        type(value) is not str
        or len(value) != 64
        or any(item not in "0123456789abcdef" for item in value)
    ):
        raise ValueError(f"{label} must be lowercase SHA-256")


__all__ = ["ResolvedBasisApplication", "ResolvedBasisTargetBinding"]
