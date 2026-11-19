"""

Loads spectral-reference values and selects the components needed for an image.

Each library column describes the signal from one fluorescence source across the
measured bands. With eight bands and six components, the reference matrix has
shape (8, 6). Columns are selected using reviewed component or fluorophore names;
all band rows remain in their acquisition order.

The library also retains column sums used to convert fitted amplitudes to total
modeled intensity across the bands. Component names, dyes and biological markers
are stored separately so a dye name does not become an inferred antibody target.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from mif_pipeline.io.metadata.canonical import is_autofluorescence_component as _is_autofluorescence

from dataclasses import dataclass
import hashlib
from importlib.resources import files
from importlib.resources.abc import Traversable
import json
from pathlib import Path
from collections.abc import Sequence
from typing import Any

import numpy as np
from numpy.typing import NDArray

# ==============================================================================
#                                    Library
# ==============================================================================


class SpectralLibraryError(ValueError):
    """

    Reports a library that cannot be read or selected unambiguously for spectral fitting.
    """

_MOTIF_RESOURCE_NAME = "PhenoImager_HT_spectral_library.umxlib"


@dataclass(frozen=True, slots=True)
class SpectralLibrary:
    """

    Stores the reference spectra together with the names associated with each row and column.

    The loaders preserve exported values and make the matrix and column-sum arrays
    read-only. Selecting columns retains their order in the original library, which
    keeps the component order consistent across images using the same selection.

    Attributes:
        identifier: Library filename or source label.
        sha256: Checksum of the original library file.
        band_names: Measurement names in matrix-row order.
        component_names: Fluorescence-source names in matrix-column order.
        fluorophores: Dye identity for each component, or None when absent.
        biomarkers: Explicit biological marker for each component, or None.
        matrix: Float64 array with shape (bands, components).
        column_sums: Sum across measurement bands for each component.
        autofluorescence_components: Column positions recognized as autofluorescence.
    """

    identifier: str
    sha256: str
    band_names: tuple[str, ...]
    component_names: tuple[str, ...]
    fluorophores: tuple[str | None, ...]
    biomarkers: tuple[str | None, ...]
    matrix: NDArray[np.float64]
    column_sums: NDArray[np.float64]
    autofluorescence_components: tuple[int, ...]

    @property
    def band_count(self) -> int:
        """

        Returns the number of measurement bands represented by the library rows.

        Returns:
            The length of band_names.
        """

        return len(self.band_names)

    @property
    def component_count(self) -> int:
        """

        Returns the number of fluorescence components represented by the selected columns.

        Returns:
            The length of component_names.
        """

        return len(self.component_names)

    def select_fluorophores(
        self,
        fluorophores: Sequence[str],
    ) -> SpectralLibrary:
        """

        Selects the unique reference column matching each requested fluorophore.

        Matching uses the literal OME Channel.Fluor name, including capitalization. A dye
        must identify exactly one column. Selected columns follow the library's order;
        for example, requesting ("Opal 690", "DAPI") from a DAPI-first library still
        produces DAPI first. Repeating a requested dye does not duplicate a fitted component.

        Args:
            fluorophores: Fluorophore names required by the reviewed image.

        Returns:
            A spectral library containing the selected component columns.

        Raises:
            SpectralLibraryError: A name is invalid, missing or ambiguous, or the selected
                spectra cannot support a full-column-rank fit.
        """

        requested = tuple(_exact_channel_fluor(value) for value in fluorophores)
        if not requested:
            raise SpectralLibraryError(
                "OME metadata has no Channel.fluor identities for spectral selection"
            )

        # Repeated requested dyes select one fitted component per unique library column.
        requested_set = set(requested)
        indices: list[int] = []
        for fluorophore in dict.fromkeys(requested):
            matches = tuple(
                index
                for index, candidate in enumerate(self.fluorophores)
                if candidate == fluorophore
            )
            if not matches:
                raise SpectralLibraryError(
                    f"OME Channel.fluor {fluorophore!r} was not found exactly "
                    "in the spectral library"
                )
            if len(matches) != 1:
                raise SpectralLibraryError(
                    f"OME Channel.fluor {fluorophore!r} matches multiple "
                    "spectral-library components"
                )

        # Select by scanning the library so output components follow its established order.
        indices.extend(
            index
            for index, fluorophore in enumerate(self.fluorophores)
            if fluorophore in requested_set
        )
        return self._select_indices(tuple(indices))

    def select_components(self, component_names: Sequence[str]) -> SpectralLibrary:
        """

        Selects reference columns by their component names, including any requested AF source.

        Trim surrounding whitespace, reject repeated requests, and retain selected columns
        in library order. The selected matrix must have enough independent measurement rows
        to distinguish its component columns.

        Args:
            component_names: Unique nonempty component names to retain.

        Returns:
            The selected reference matrix and corresponding component metadata.

        Raises:
            SpectralLibraryError: Names are invalid, repeated or absent, or the selected
                columns are linearly dependent or outnumber the bands.
        """

        requested = tuple(_required_name(value, "canonical component") for value in component_names)
        if not requested:
            raise SpectralLibraryError("canonical component order must not be empty")
        _reject_duplicates(requested, "canonical component")
        requested_set = set(requested)
        for component in requested:
            if component not in self.component_names:
                raise SpectralLibraryError(
                    f"canonical component {component!r} was not found exactly "
                    "in the spectral library"
                )
        indices = tuple(
            index
            for index, component in enumerate(self.component_names)
            if component in requested_set
        )
        return self._select_indices(indices)

    def _select_indices(self, indices: tuple[int, ...]) -> SpectralLibrary:
        """

        Copies selected matrix columns and updates their associated names and AF positions.

        Check full column rank before returning the selection. This requires enough
        independent spectral information to separate the selected components. AF positions
        are then renumbered for the smaller matrix; column 7 of a full library may become
        column 2 after selecting three components.

        Args:
            indices: Catalogue column positions already resolved by the name selector.

        Returns:
            A SpectralLibrary containing the selected values, names and reporting weights.

        Raises:
            SpectralLibraryError: Columns outnumber the bands or are linearly dependent.
        """

        matrix = np.asarray(self.matrix[:, indices], dtype=np.float64)
        if matrix.shape[1] > matrix.shape[0]:
            raise SpectralLibraryError("selected spectral basis has more components than bands")
        if np.linalg.matrix_rank(matrix) != matrix.shape[1]:
            raise SpectralLibraryError("selected spectral basis does not have full column rank")
        matrix.setflags(write=False)
        column_sums = np.asarray(self.column_sums[list(indices)], dtype=np.float64)
        column_sums.setflags(write=False)

        # Recalculate AF column positions for the selected component matrix.
        remapped_af = tuple(
            selected_index
            for selected_index, source_index in enumerate(indices)
            if source_index in self.autofluorescence_components
        )
        return SpectralLibrary(
            identifier=self.identifier,
            sha256=self.sha256,
            band_names=self.band_names,
            component_names=tuple(self.component_names[index] for index in indices),
            fluorophores=tuple(self.fluorophores[index] for index in indices),
            biomarkers=tuple(self.biomarkers[index] for index in indices),
            matrix=matrix,
            column_sums=column_sums,
            autofluorescence_components=remapped_af,
        )


def load_spectral_library(path: str | Path) -> SpectralLibrary:
    """

    Reads a local JSON spectral library and parses its band names and component spectra.

    The same file bytes supply both the numerical values and the source checksum.

    Args:
        path: Location of the exported library file.

    Returns:
        The complete SpectralLibrary, ready for component selection.

    Raises:
        OSError: The file cannot be read.
        SpectralLibraryError: The JSON or required library content is invalid.
    """

    source = Path(path)
    payload_bytes = source.read_bytes()
    return _load_spectral_library_bytes(payload_bytes, identifier=source.name)


def motif_library_resource() -> Traversable:
    """

    Locates the PhenoImager MOTIF library included with the installed Python package.

    Returns:
        A package-resource handle from which the library bytes can be read.
    """

    return files(__package__).joinpath("data").joinpath(_MOTIF_RESOURCE_NAME)


def load_motif_spectral_library() -> SpectralLibrary:
    """

    Reads the packaged PhenoImager MOTIF library through the shared spectral-library parser.

    Returns:
        The complete catalogue of exported spectra and component descriptions.
    """

    resource = motif_library_resource()
    return _load_spectral_library_bytes(
        resource.read_bytes(),
        identifier=resource.name,
    )


def _load_spectral_library_bytes(
    payload_bytes: bytes,
    *,
    identifier: str,
) -> SpectralLibrary:
    """

    Parses the exported component spectra into a band-by-component reference matrix.

    Each entry in "spectra" supplies one component vector with a value for every
    measurement band. Stack these vectors, then transpose so the solver receives bands
    in rows and components in columns. Require finite matrix values and a positive
    spectral sum for each column. Rank is checked later on the selected components.

    Args:
        payload_bytes: Complete JSON library contents.
        identifier: Source label retained with the parsed library and its errors.

    Returns:
        A SpectralLibrary with the matrix, component descriptions, AF positions,
        column sums and source checksum.

    Raises:
        SpectralLibraryError: JSON structure, names, vector lengths or values are invalid.
    """

    try:
        payload = json.loads(payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpectralLibraryError(f"invalid spectral library JSON: {identifier}") from exc
    if not isinstance(payload, dict):
        raise SpectralLibraryError("spectral library root must be an object")
    bands = _acquisition_band_names(payload)
    spectra = payload.get("spectra")
    if not isinstance(spectra, list) or not spectra:
        raise SpectralLibraryError("spectral library requires a non-empty spectra list")

    component_names: list[str] = []
    fluorophores: list[str | None] = []
    biomarkers: list[str | None] = []
    columns: list[list[float]] = []
    for index, entry in enumerate(spectra):
        if not isinstance(entry, dict):
            raise SpectralLibraryError(f"spectrum {index} must be an object")
        fluor = _optional_name(entry.get("fluor"), f"spectrum {index} fluor")
        component = _optional_name(entry.get("component", fluor), f"spectrum {index} component")
        if component is None:
            raise SpectralLibraryError(f"spectrum {index} has no component identity")
        marker = _optional_name(entry.get("marker"), f"spectrum {index} marker")
        values = entry.get("spectrum")
        if not isinstance(values, list) or len(values) != len(bands):
            raise SpectralLibraryError(
                f"spectrum {component!r} length must equal {len(bands)} bands"
            )
        try:
            column = [float(value) for value in values]
        except (TypeError, ValueError) as exc:
            raise SpectralLibraryError(
                f"spectrum {component!r} contains a non-numeric value"
            ) from exc
        component_names.append(component)
        fluorophores.append(fluor)
        biomarkers.append(marker)
        columns.append(column)
    _reject_duplicates(component_names, "component")

    # Transpose the exported component vectors to place measurements in rows and components in
    # columns.
    matrix = np.asarray(columns, dtype=np.float64).T
    if not np.all(np.isfinite(matrix)):
        raise SpectralLibraryError("spectral matrix values must be finite")
    matrix.setflags(write=False)

    # Keep each exported column scale. Its sum will be applied after fitting
    # when reporting spectral-total component intensities.
    column_sums = np.sum(matrix, axis=0, dtype=np.float64)
    if np.any(column_sums <= 0):
        raise SpectralLibraryError("spectral library column sums must be strictly positive")
    column_sums.setflags(write=False)
    af_components = tuple(
        index for index, name in enumerate(component_names) if _is_autofluorescence(name)
    )
    return SpectralLibrary(
        identifier=identifier,
        sha256=hashlib.sha256(payload_bytes).hexdigest(),
        band_names=bands,
        component_names=tuple(component_names),
        fluorophores=tuple(fluorophores),
        biomarkers=tuple(biomarkers),
        matrix=matrix,
        column_sums=column_sums,
        autofluorescence_components=af_components,
    )


def _unique_names(value: Any, label: str) -> tuple[str, ...]:
    """

    Reads a nonempty JSON list of names and rejects duplicates after trimming whitespace.

    Args:
        value: List of names to parse.
        label: Name category used in errors, such as "band".

    Returns:
        The cleaned names as a tuple in their declared order.

    Raises:
        SpectralLibraryError: The list or one of its names is invalid or repeated.
    """

    if not isinstance(value, list) or not value:
        raise SpectralLibraryError(f"spectral library requires non-empty {label}s")
    result = tuple(_required_name(item, label) for item in value)
    _reject_duplicates(result, label)
    return result


def _acquisition_band_names(payload: dict[str, Any]) -> tuple[str, ...]:
    """

    Reads the band-name list that defines the reference matrix's row order.

    The format accepts either "acquisition_bands" or "bands". Exactly one must supply
    the list, ensuring there is a single declared measurement order.

    Args:
        payload: Decoded library object.

    Returns:
        Ordered, unique measurement-band names.

    Raises:
        SpectralLibraryError: Both lists are present, the list is missing, or names are invalid.
    """

    has_explicit = "acquisition_bands" in payload
    has_legacy = "bands" in payload
    if has_explicit and has_legacy:
        raise SpectralLibraryError(
            "spectral library must not define both acquisition_bands and bands"
        )
    key = "acquisition_bands" if has_explicit else "bands"
    return _unique_names(payload.get(key), "band")


def _required_name(value: Any, label: str) -> str:
    """

    Reads required nonblank text and trims surrounding whitespace.

    Args:
        value: Name to parse.
        label: Field description used in errors.

    Returns:
        The cleaned name.

    Raises:
        SpectralLibraryError: The value is missing, blank or not text.
    """

    result = _optional_name(value, label)
    if result is None:
        raise SpectralLibraryError(f"{label} identity must be non-empty text")
    return result


def _exact_channel_fluor(value: Any) -> str:
    """

    Checks a fluorophore identity exactly as recorded in the reviewed image.

    Surrounding whitespace is rejected so matching cannot silently change the
    Channel.Fluor value selected by the researcher.

    Args:
        value: Literal fluorophore name from the image metadata.

    Returns:
        The valid fluorophore identity.

    Raises:
        SpectralLibraryError: The name is missing, blank, non-text or padded with whitespace.
    """

    if not isinstance(value, str) or not value.strip():
        raise SpectralLibraryError("OME Channel.fluor identity must be non-empty text")
    if value != value.strip():
        raise SpectralLibraryError("OME Channel.fluor has leading or trailing whitespace")
    return value


def _optional_name(value: Any, label: str) -> str | None:
    """

    Reads an optional dye or marker name, retaining None when the field is absent.

    Args:
        value: Name text or None.
        label: Field description used in errors.

    Returns:
        Trimmed nonempty text, or None.

    Raises:
        SpectralLibraryError: A supplied value is blank or not text.
    """

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SpectralLibraryError(f"{label} identity must be non-empty text or null")
    return value.strip()


def _reject_duplicates(values: list[str] | tuple[str, ...], label: str) -> None:
    """

    Checks that names identify distinct library entries.

    Args:
        values: Parsed names to inspect.
        label: Name category used in the error message.

    Raises:
        SpectralLibraryError: A name occurs more than once.
    """

    if len(values) != len(set(values)):
        raise SpectralLibraryError(f"duplicate {label} identity in spectral library")
