"""Non-numerical channel inventory for copying images ready for analysis.

This is deliberately not a spectral library: it contains no spectra, matrix,
normalization factors or invented calibration. The v3 copy provenance records
library and solver as not applied."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from dataclasses import dataclass
from ome_types import OME
from mif_pipeline.io import ResolvedSpectralState

# ==============================================================================
#                                 Copy semantics
# ==============================================================================


@dataclass(frozen=True, slots=True)
class UnmixedComponentInventory:
    component_names: tuple[str, ...]
    identifier: None = None
    sha256: None = None
    band_names: tuple[str, ...] = ()
    column_sums: tuple[float, ...] = ()
    autofluorescence_components: tuple[int, ...] = ()

    @property
    def component_count(self) -> int:
        return len(self.component_names)


def copy_component_inventory(ome: OME, state: ResolvedSpectralState) -> UnmixedComponentInventory:
    from mif_pipeline.io.metadata.spectral import resolve_prepared_channel_order

    names = state.component_order if state.state == "unmixed" else resolve_prepared_channel_order(ome, 0)
    if not names or len(names) != ome.images[0].pixels.size_c:
        raise ValueError(
            "Prepared images require the reviewed identity of every channel"
        )
    if any(not isinstance(n, str) or not n.strip() for n in names):
        raise ValueError("Already-unmixed component labels must be nonempty")
    return UnmixedComponentInventory(tuple(names))
