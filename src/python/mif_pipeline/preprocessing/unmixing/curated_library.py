"""User-declared numerical spectra, distinct from reference-image evidence.

The `CuratedSpectralLibrary` record carries `band_names`, `component_names`, `values`,
`normalization`, `measurement_convention` and its remaining declared fields.

The functions retain the selected spectral basis, component order and reviewed acquisition
facts through the operation. Numerical work is bounded by the configured pixel blocks, while
published references and results keep their input and settings identities."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from mif_pipeline.io.metadata.canonical import is_autofluorescence_component
from mif_pipeline.io.metadata.cycles import split_cycle_component, resolve_channel_cycles
from dataclasses import dataclass, field
import csv, io, json, math, hashlib
import numpy as np
from .typed_checksum import typed_checksum_sha256

# ==============================================================================
#                                Curated library
# ==============================================================================


@dataclass(frozen=True, slots=True)
class CuratedSpectralLibrary:
    band_names: tuple[str, ...]
    component_names: tuple[str, ...]
    values: tuple[tuple[float, ...], ...]
    normalization: str
    measurement_convention: str
    content_sha256: str = field(init=False)

    def __post_init__(self):
        for names in (self.band_names, self.component_names):
            if (
                type(names) is not tuple
                or not 1 <= len(names) <= 64
                or len(set(names)) != len(names)
                or any(type(v) is not str or not v or v != v.strip() for v in names)
            ):
                raise ValueError(
                    "Spectral channel and component names must be unique, nonempty labels"
                )
        if self.normalization not in {"l1", "l2"}:
            raise ValueError("Select unit-sum (L1) or unit-length (L2) reference spectra")
        if self.measurement_convention not in {"acquisition_normalized", "exposure_normalized", "stored_calibrated", "target_raw_acquisition"}:
            raise ValueError("Declare the intensity-correction convention of the supplied spectra")
        if type(self.values) is not tuple or any(
            type(row) is not tuple or any(type(v) not in (int, float) for v in row)
            for row in self.values
        ):
            raise ValueError("Curated spectra must contain immutable numerical rows")
        matrix = np.asarray(self.values, dtype=float)
        if (
            matrix.shape != (len(self.band_names), len(self.component_names))
            or not np.all(np.isfinite(matrix))
            or np.any(matrix < 0)
        ):
            raise ValueError(
                "Spectra require one finite nonnegative value per measurement and component"
            )
        # A shared cyclic library can contain more dyes than any one cycle uses.
        # Identifiability is checked against the actual selected cycle components
        # in validate_curated_target, before this library can be accepted.
        norm = matrix.sum(axis=0) if self.normalization == "l1" else np.linalg.norm(matrix, axis=0)
        if not np.allclose(norm, 1, rtol=1e-3, atol=1e-6):
            raise ValueError(
                "The supplied columns do not match the selected normalization convention"
            )
        object.__setattr__(
            self,
            "content_sha256",
            typed_checksum_sha256(
                {
                    "schema_version": "curated-spectral-library/v1",
                    "band_names": list(self.band_names),
                    "component_names": list(self.component_names),
                    "values": [list(row) for row in self.values],
                    "normalization": self.normalization,
                    "measurement_convention": self.measurement_convention,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class CuratedSourceDecision:
    source_checkpoint_sha256: str
    target_input_sha256: str
    target_image_sha256: str
    library: CuratedSpectralLibrary
    reviewer_id: str
    content_sha256: str = field(init=False)

    def __post_init__(self):
        for value in (
            self.source_checkpoint_sha256,
            self.target_input_sha256,
            self.target_image_sha256,
        ):
            if (
                type(value) is not str
                or len(value) != 64
                or any(c not in "0123456789abcdef" for c in value)
            ):
                raise ValueError("Curated source decision lacks exact input identity")
        if (
            type(self.library) is not CuratedSpectralLibrary
            or type(self.reviewer_id) is not str
            or not self.reviewer_id.strip()
        ):
            raise ValueError("Curated source review is incomplete")
        self.library.__post_init__()
        object.__setattr__(
            self,
            "content_sha256",
            typed_checksum_sha256(
                {
                    "source_checkpoint": self.source_checkpoint_sha256,
                    "target_input": self.target_input_sha256,
                    "target_image": self.target_image_sha256,
                    "library": self.library.content_sha256,
                    "reviewer": self.reviewer_id,
                }
            ),
        )


def _spectral_label_order(supplied, expected, *, components=False):
    """Match exact labels once and return their positions in the requested order.

    Existing AF aliases describe the same AF column. Dye and acquired-band labels
    retain their exact spelling. We reject absent, repeated or additional labels
    before moving any values.
    """
    kind = "output components" if components else "acquired bands"
    guidance = ("Correct the component names in the library or review Channels and markers."
                if components else "Choose or correct a library with the acquired-band labels shown in the spectral table.")
    if not supplied or any(not isinstance(name, str) or not name or name != name.strip()
                           for name in supplied):
        raise ValueError(f"Check the spectral references: Give the {kind} nonempty labels without surrounding spaces. {guidance}")
    def key(name):
        return ("af", split_cycle_component(name)[1]) if components and is_autofluorescence_component(name) else ("label", name)
    positions = {}
    duplicates = []
    for index, name in enumerate(supplied):
        identity = key(name)
        if identity in positions:
            duplicates.append(name)
        else:
            positions[identity] = index
    if duplicates:
        raise ValueError(f"Check the spectral references: Repeated {kind}: {', '.join(duplicates)}. Keep one entry for each. {guidance}")
    if expected is None:
        return tuple(range(len(supplied)))
    expected = tuple(expected)
    expected_keys = tuple(key(name) for name in expected)
    expected_set = set(expected_keys)
    if not expected or len(expected_set) != len(expected_keys):
        raise ValueError(f"Check the spectral references: The reviewed {kind} do not have distinct labels. {guidance}")
    missing = [name for name, identity in zip(expected, expected_keys, strict=True) if identity not in positions]
    extras = [name for name in supplied if key(name) not in expected_set]
    if missing or extras:
        detail = []
        if missing:
            detail.append(f"Missing {kind}: {', '.join(missing)}.")
        if extras:
            detail.append(f"Unexpected {kind}: {', '.join(extras)}.")
        raise ValueError("Check the spectral references: " + " ".join((*detail, guidance)))
    return tuple(positions[identity] for identity in expected_keys)


def read_spectral_csv_table(text, *, band_names, component_names=None):
    """Read named spectral rows and align their values to the reviewed image panel.

    Rows follow the acquired-band order. When component names are supplied,
    columns follow the reviewed output order. Cell text is retained so the desktop
    can restore unfinished entries; scientific validation follows on submission.
    """
    if type(text) is not str or len(text.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("Check the spectral references: Enter a spectral table smaller than 2 MiB.")
    try:
        rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff")), strict=True))
    except csv.Error as error:
        raise ValueError("Check the spectral references: The spectral CSV could not be read. Check its commas and quoted cells.") from error
    if not rows or not rows[0] or rows[0][0] != "measurement":
        raise ValueError("Check the spectral references: Use the spectral CSV heading: measurement, followed by component names.")
    components = tuple(rows[0][1:])
    if len(components) > 64:
        raise ValueError("Check the spectral references: The spectral table supports up to 64 output components.")
    if any(len(row) != len(components) + 1 for row in rows[1:]):
        raise ValueError("Check the spectral references: Each acquired-band row needs its label and one value for every output component.")
    row_order = _spectral_label_order(tuple(row[0] for row in rows[1:]), tuple(band_names))
    column_order = _spectral_label_order(components, component_names, components=True)
    return (
        tuple(components[index] for index in column_order),
        tuple(tuple(rows[index + 1][column + 1] for column in column_order) for index in row_order),
    )


def parse_spectral_csv(
    text, *, band_names, normalization, measurement_convention,
    component_names=None, allow_cycle_subsets=False,
):
    """Validate supplied spectra after matching their named rows and columns."""
    components, cells = read_spectral_csv_table(
        text, band_names=band_names, component_names=component_names)
    try:
        values = tuple(tuple(float(value) for value in row) for row in cells)
    except ValueError as error:
        raise ValueError("Check the spectral references: Complete every spectral value with a number.") from error
    library = CuratedSpectralLibrary(
        tuple(band_names), components, values, normalization,
        measurement_convention,
    )
    if not allow_cycle_subsets:
        _require_identifiable_matrix(np.asarray(library.values))
    return library


def _require_identifiable_matrix(matrix):
    if matrix.shape[1] > matrix.shape[0] or np.linalg.matrix_rank(matrix) != matrix.shape[1]:
        raise ValueError(
            "The reference matrix must contain distinguishable components and at least as many measurement bands"
        )


def validate_curated_target(library, target, af_behavior="retain"):
    if type(library) is not CuratedSpectralLibrary:
        raise ValueError("The curated library measurement bands do not match this image")
    from .cycle_references import expand_shared_curated_library
    library = expand_shared_curated_library(library, target, af_behavior)
    mode = target.evidence.quantitative_signature.measurements[0].correction_mode
    allowed_conventions = {
        "full": {"acquisition_normalized", "target_raw_acquisition"},
        "exposure_only": {"exposure_normalized", "target_raw_acquisition"},
        "as_stored": {"stored_calibrated"},
    }[mode]
    if library.measurement_convention not in allowed_conventions:
        raise ValueError(
            "The supplied spectra use a different intensity convention. Reference spectra "
            "must match the image's imaging settings and correction procedure."
        )
    dyes = tuple(
        dict.fromkeys(name for name in target.component_names if name is not None and not is_autofluorescence_component(name))
    )
    af = tuple(name for name in library.component_names if is_autofluorescence_component(name))
    declared_dyes = tuple(n for n in library.component_names if n not in af)
    if af_behavior not in {"retain", "already_removed"}:
        raise ValueError("The target AF choice is unsupported")
    cycles = tuple(dict.fromkeys(split_cycle_component(name)[1] for name in declared_dyes))
    expected_af_cycles = set(cycles)
    if af_behavior == "retain" and (
        len(af) != len(expected_af_cycles)
        or {split_cycle_component(name)[1] for name in af} != expected_af_cycles
        or library.component_names[-len(af):] != af
    ):
        raise ValueError("Declare fluorophore components followed by one AF component for each imaging cycle")
    if af_behavior == "already_removed" and af:
        raise ValueError("For AF already removed, supply fluorophore components only")
    if not set(dyes).issubset(declared_dyes) or (
        (target.component_inventory is not None or all(name is not None for name in target.channel_fluors)) and declared_dyes != dyes
    ):
        raise ValueError("The library components differ from the image's known fluorophore panel")
    reviewed_xml = getattr(target.locator_binding, "reviewed_ome_xml", None)
    if reviewed_xml is not None:
        from ome_types import from_xml
        from .cyclic import cycle_solve_groups
        channel_cycles = resolve_channel_cycles(from_xml(reviewed_xml), require_complete=True)
        cycle_solve_groups(channel_cycles, library.component_names, np.asarray(library.values))
    _require_identifiable_matrix(np.asarray(library.values))
    return library


def propose_curated_basis(decision, target, run_id, source_checkpoint_sha256, qc_policy, af_behavior="retain"):
    from .reference_models import BasisSourceLineage, ResolvedBasisColumn
    from .final_basis import (
        FinalBasisSourceBinding,
        FinalBasisCompatibilityEvidence,
        FinalRunBasisProposal,
    )
    from .target_af import evaluate_run_basis_qc

    supplied_library = decision.library
    library = validate_curated_target(supplied_library, target, af_behavior)
    if (
        decision.target_input_sha256 != target.authority_sha256
        or decision.target_image_sha256 != target.evidence.source_image_sha256
    ):
        raise ValueError("The curated reference was reviewed for different imaging settings")
    matrix = np.asarray(library.values, dtype=float).copy()
    if library.measurement_convention == "target_raw_acquisition":
        for i, m in enumerate(target.evidence.quantitative_signature.measurements):
            matrix[i] *= (
                1.0 / m.exposure_seconds if m.correction_mode == "exposure_only" else
                m.response_correction_multiplier / (m.effective_digital_maximum * m.gain * m.binning_area * m.exposure_seconds)
            )
        divisor = (matrix.sum(axis=0) if library.normalization == "l1"
                   else np.linalg.norm(matrix, axis=0))
        matrix /= divisor
    lineage = BasisSourceLineage(
        "curated_spectral_library",
        "curated:" + supplied_library.content_sha256,
        supplied_library.content_sha256,
        None,
    )
    columns = tuple(
        ResolvedBasisColumn(
            name,
            None if is_autofluorescence_component(name) else split_cycle_component(name)[0],
            "profile_autofluorescence" if is_autofluorescence_component(name) else "fluorophore",
            tuple(float(x) for x in matrix[:, i]),
            "unit_l1" if library.normalization == "l1" else "unit_l2_spectral_shape_v1",
            1.0,
            lineage,
        )
        for i, name in enumerate(library.component_names)
    )
    binding = FinalBasisSourceBinding(
        "curated_spectral_library", lineage.artifact_id, lineage.content_sha256
    )
    compatibility = FinalBasisCompatibilityEvidence(
        "curated_spectra", (), (), None, (supplied_library.content_sha256,)
    )
    return FinalRunBasisProposal(
        source_kind="curated_spectra",
        source_choice="curated_reference_library",
        af_behavior=af_behavior,
        source_checkpoint_sha256=source_checkpoint_sha256,
        channel_fluors=library.component_names,
        run_id=run_id,
        target_image_sha256=target.evidence.source_image_sha256,
        axis=target.evidence.axis,
        columns=columns,
        qc_policy=qc_policy,
        qc=evaluate_run_basis_qc(columns, target.evidence.axis, qc_policy),
        compatibility=compatibility,
        source_bindings=(binding,),
        quantitative_signature=target.evidence.quantitative_signature,
        ssasu_context=None,
    )
