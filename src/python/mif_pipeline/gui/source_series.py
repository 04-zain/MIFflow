"""Read-only source-series inspection boundary for desktop presentation."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from mif_pipeline import io
from mif_pipeline.io import SourceSeriesInventory

# ==============================================================================
#                                 Source series
# ==============================================================================


class SourceSeriesInspectionPort(Protocol):
    """Provides the IO-owned inventory for one selected local source."""

    def inspect_source_series(self, source_path: Path, *, require_planar_channels: bool = True) -> SourceSeriesInventory: ...


class LocalSourceSeriesInspectionService:
    """Delegates source inspection to the public IO owner."""

    def __init__(self, require_image_runtime_selection: Callable[[], None] | None = None) -> None:
        self._require_image_runtime_selection = require_image_runtime_selection
        self._last_inspection: tuple[tuple[object, ...], SourceSeriesInventory] | None = None

    def inspect_source_series(self, source_path: Path, *, require_planar_channels: bool = True) -> SourceSeriesInventory:
        """Reuse unchanged QPTIFF inventories; inspect other formats afresh."""
        if self._require_image_runtime_selection is not None:
            self._require_image_runtime_selection()
        if type(require_planar_channels) is not bool:
            raise ValueError("planar-channel requirement must be boolean")
        path = source_path.resolve(strict=True)
        # QPTIFF stores its image and acquisition metadata in one file. Other
        # reader formats may depend on companions or directory members.
        if not path.is_file() or path.suffix.casefold() != ".qptiff":
            self._last_inspection = None
            return io.inspect_source_series_inventory(
                path, require_planar_channels=require_planar_channels, allow_pending_organization=True,
            )

        def freshness() -> tuple[object, ...]:
            details = path.stat()
            return (path, details.st_dev, details.st_ino, details.st_size,
                    details.st_mtime_ns, details.st_ctime_ns, require_planar_channels)

        before = freshness()
        retained = self._last_inspection
        if retained is not None and retained[0] == before:
            return retained[1]
        self._last_inspection = None
        inventory = io.inspect_source_series_inventory(
            path, require_planar_channels=require_planar_channels, allow_pending_organization=True,
        )
        if freshness() != before:
            raise ValueError("The source image changed during inspection. Inspect it again.")
        # This only retains a displayed choice in this desktop session. The
        # scientific ingestion path independently reopens and hashes the source.
        self._last_inspection = (before, inventory)
        return inventory


__all__ = ["SourceSeriesInspectionPort", "LocalSourceSeriesInspectionService"]
