"""Map explicitly submitted local file controls to their mounted cluster locations."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from collections.abc import Mapping
from .interactions import ControlKind

# ==============================================================================
#                               Cluster form paths
# ==============================================================================


def map_response_paths(form, values, response, mounted):
    selected = {
        c.name: values.get(c.name) for c in form.controls if c.kind is ControlKind.LOCAL_FILE
    }
    selected = {name: value for name, value in selected.items() if isinstance(value, str) and value}
    allowed_keys = set(selected) | {"path", "locator"}

    def map_value(value, key=None):
        if (
            key == "model_path"
            and form.context.get("adapter") == "segmentation_setup"
            and form.context.get("remote_execution")
        ):
            # This location came from cluster Software setup, not a mounted-file control.
            return value
        if isinstance(value, Mapping):
            return {name: map_value(item, name) for name, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [map_value(item) for item in value]
        if key in allowed_keys and isinstance(value, str) and value in selected.values():
            if mounted is None:
                raise ValueError(
                    "Configure the mounted cluster folder before selecting a file for this review."
                )
            try:
                return mounted.cluster_path(value)
            except ValueError as error:
                raise ValueError(
                    "Select the reference or model from the mounted cluster folder so the cluster can read the same file."
                ) from error
        from .cluster_paths import cluster_response_values

        return cluster_response_values(value, mounted, _key=key)

    return map_value(response)
