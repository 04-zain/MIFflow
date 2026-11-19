"""Map explicit mounted-file selections; review identities stay unchanged."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from collections.abc import Mapping
from pathlib import PureWindowsPath
import re

# ==============================================================================
#                                 Cluster paths
# ==============================================================================


def cluster_response_values(value, mounted, *, _key=None):
    if isinstance(value, Mapping):
        return {
            key: cluster_response_values(item, mounted, _key=key) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [cluster_response_values(item, mounted, _key=_key) for item in value]
    path_field = isinstance(_key, str) and (
        _key in {"path", "file", "locator", "directory"}
        or _key.endswith(("_path", "_file", "_locator", "_directory", "_paths", "_files"))
    )
    if not path_field or not isinstance(value, str) or mounted is None:
        return value
    try:
        return mounted.cluster_path(value)
    except ValueError:
        if re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\"):
            raise ValueError(
                "Choose the file from the mounted cluster folder before submitting it to the cluster."
            )
        return value


def with_selected_kronos_model(profile, mounted):
    """Refresh the compute-host model path from the current Software setup choice.

    Only the path crosses the connection. Package inspection and worker selection
    use the cluster installation when it builds the review form.
    """
    from dataclasses import replace
    from mif_pipeline.orchestration.setup.kronos_setup import selected_package, selected_marker_text_model

    selected = selected_package()
    remote = None
    if selected is not None:
        try:
            if mounted is None:
                raise ValueError("No mounted cluster folder is configured.")
            remote = mounted.cluster_path(str(selected))
        except ValueError as error:
            raise ValueError("For streaming KRONOS analysis, select its model folder from the mounted cluster folder in Software setup, then save Computing again.") from error
    text = selected_marker_text_model()
    remote_text = None
    if text is not None:
        try:
            if mounted is None:
                raise ValueError("No mounted cluster folder is configured")
            remote_text = {**text.to_mapping(), "directory": mounted.cluster_path(text.directory)}
        except ValueError:
            # This optional helper is required only when a selected panel adds
            # markers. Never send an unmapped workstation path to the host.
            remote_text = None
    return replace(profile, kronos_package_directory=remote, kronos_marker_text_model=remote_text)
