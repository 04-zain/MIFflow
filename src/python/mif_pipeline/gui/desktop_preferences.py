"""Public interface for the shared installed-software selection record."""

# ==============================================================================
#                                    Imports
# ==============================================================================

import os
from mif_pipeline.pipeline_core.software_preferences import (
    DesktopPreferencesV1,
    desktop_preferences_path,
    load_desktop_preferences,
    write_desktop_preferences,
)

__all__ = [
    "DesktopPreferencesV1",
    "desktop_preferences_path",
    "load_desktop_preferences",
    "write_desktop_preferences",
]
