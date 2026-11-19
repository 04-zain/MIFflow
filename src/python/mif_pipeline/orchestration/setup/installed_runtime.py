"""Discover installed QuPath components lazily; never acquire software during analysis."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from contextlib import contextmanager
from pathlib import Path
import os, tempfile
from mif_pipeline.qupath_runtime.class_origin_candidate import probe_qupath_class_origins
from mif_pipeline.io import selected_offline_bioformats_inventory
from mif_pipeline.io.bioformats_prerequisite import InventoryMissing
from .prerequisite_setup import default_cpu_setup_target
from ..prototype_owner_inputs import prototype_owner_inputs_supplier

# ==============================================================================
#                               Installed runtime
# ==============================================================================


def apply_saved_software(args, preferences, *, environment=None):
    if preferences is None:
        return
    env = os.environ if environment is None else environment

    def fill(field, value, variable):
        if getattr(args, field, None) is None and not env.get(variable) and value is not None:
            setattr(args, field, Path(value))

    fill("qupath", preferences.qupath_executable, "MIF_QUPATH_EXECUTABLE")
    selected = getattr(args, "qupath", None) or env.get("MIF_QUPATH_EXECUTABLE")
    if selected is not None and Path(selected) == Path(preferences.qupath_executable):
        fill("qupath_profile", preferences.qupath_profile_manifest, "MIF_QUPATH_PROFILE")
    fill("java_home", preferences.java_home, "JAVA_HOME")
    fill("bioformats_root", preferences.bioformats_root, "MIF_BIOFORMATS_PAYLOAD_ROOT")


@contextmanager
def installed_runtime_inputs(executable, *, platform, launch_binding=None):
    cache = None
    supplier = None

    def inputs(manifest, plan):
        nonlocal cache, supplier
        if manifest.normalized_manifest_sha256 != plan.normalized_manifest_sha256:
            raise ValueError("The prerequisite check does not match the current analysis plan.")
        if supplier is None:
            try:
                inventory = selected_offline_bioformats_inventory()
            except InventoryMissing:
                inventory = None
            origins = probe_qupath_class_origins(executable, launch_binding=launch_binding)
            paths = {
                name: (
                    getattr(origins, name).code_source
                    if getattr(origins, name).status == "candidate"
                    else None
                )
                for name in ("instanseg", "djl", "pytorch_engine", "warpy")
                if hasattr(origins, name)
            }
            cache = tempfile.TemporaryDirectory(prefix="mif-qupath-probe-")
            supplier = prototype_owner_inputs_supplier(
                qupath_executable=executable,
                instanseg_jar=paths.get("instanseg"),
                djl_jar=paths.get("djl"),
                pytorch_engine_jar=paths.get("pytorch_engine"),
                warpy_jar=paths.get("warpy"),
                native_root=default_cpu_setup_target(platform),
                cache_root=Path(cache.name),
                platform=platform,
                bioformats_inventory=inventory,
                launch_binding=launch_binding,
            )
        return supplier(manifest, plan)

    try:
        yield inputs
    finally:
        if cache is not None:
            cache.cleanup()
