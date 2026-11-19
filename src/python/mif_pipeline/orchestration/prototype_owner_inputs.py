"""Bind installed prototype paths to the existing batch prerequisite owners."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from mif_pipeline.io.bioformats_prerequisite import BioFormatsRuntimeInventory
from mif_pipeline.qupath_runtime.launch_binding import QuPathProfileLaunchBinding, qupath_command
from mif_pipeline.qupath_runtime.payload_lock import (
    MODEL_VERSION,
    prototype_probe_request,
)

from .batch_dag import BatchPlan
from .batch_segmentation_settings import current_segmentation_settings
from .batch_settled_review_revision import requires_paired_alignment
from .batch_manifest import ValidatedBatchManifest
from .setup.batch_prerequisites import BatchPrerequisiteOwnerInputs

# ==============================================================================
#                             Prototype owner inputs
# ==============================================================================


def prototype_owner_inputs_supplier(
    *,
    qupath_executable: Path,
    instanseg_jar: Path,
    djl_jar: Path,
    pytorch_engine_jar: Path,
    native_root: Path,
    cache_root: Path,
    platform: str,
    bioformats_inventory: BioFormatsRuntimeInventory | None,
    launch_binding: QuPathProfileLaunchBinding | None = None,
    warpy_jar: Path | None = None,
) -> Callable[[ValidatedBatchManifest, BatchPlan], BatchPrerequisiteOwnerInputs]:
    """Supply exact model and selected-profile registration probe requests."""
    qupath_command(qupath_executable, launch_binding=launch_binding)

    def inputs(
        manifest: ValidatedBatchManifest,
        plan: BatchPlan,
    ) -> BatchPrerequisiteOwnerInputs:
        if plan.normalized_manifest_sha256 != manifest.normalized_manifest_sha256:
            raise ValueError("prerequisite plan differs from validated manifest")
        requests = {}
        for sample in manifest.enabled_samples:
            models = current_segmentation_settings(manifest, plan, sample.sample_id)
            if all(p is not None for p in (instanseg_jar, djl_jar, pytorch_engine_jar)):
                keys = {(None, None, False)}
                keys.update((model.model_path, model.model_version, False)
                            for model in models if model is not None and model.model_version == MODEL_VERSION)
                for key in sorted(keys, key=lambda key: (str(key[0]), key[1] or "")):
                    if key not in requests:
                        requests[key] = prototype_probe_request(
                            executable=qupath_executable, model_root=key[0],
                            extension_jar=instanseg_jar, djl_jar=djl_jar,
                            pytorch_engine_jar=pytorch_engine_jar, native_root=native_root,
                            cache_root=cache_root, platform=platform, launch_binding=launch_binding)
            if requires_paired_alignment(sample, plan):
                from mif_pipeline.qupath_runtime.warpy_probe import WarpyPrerequisiteRequest
                from mif_pipeline.qupath_runtime.payload_lock import WARPY_SOURCE_ARCHIVE

                selected = warpy_jar or (
                    launch_binding.profile_root
                    / "extensions"
                    / WARPY_SOURCE_ARCHIVE[0].rsplit("/", 1)[1]
                    if launch_binding
                    else None
                )
                if selected is not None:
                    requests[(None, None, True)] = WarpyPrerequisiteRequest(
                        qupath_executable, selected, launch_binding
                    )
        return BatchPrerequisiteOwnerInputs(bioformats_inventory, requests)

    return inputs


__all__ = ["prototype_owner_inputs_supplier"]
