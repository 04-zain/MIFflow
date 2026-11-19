"""Typed numerical reference controls; unspecified cutoffs stay explicit inputs.

The interface keeps editable researcher choices associated with the current image or
analysis request. Values are passed to the stage-specific validator before they become
accepted processing settings."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from .interactions import ControlKind, FormControl, _json_editable_value

QC = (
    (
        "svd_absolute_tolerance",
        "Absolute matrix-rank tolerance",
        "Absolute cutoff for singular values when counting independent reference spectra. The rank check uses the larger of this cutoff and the relative cutoff.",
    ),
    (
        "svd_relative_tolerance",
        "Relative matrix-rank tolerance",
        "Fraction of the largest singular value used as the relative cutoff in the matrix-rank check.",
    ),
    (
        "cosine_review_threshold",
        "Cosine similarity requiring review",
        "Flags reference pairs whose cosine similarity reaches this value, from 0 to 1.",
    ),
    (
        "cosine_fail_threshold",
        "Maximum cosine similarity",
        "Rejects the reference matrix when a pair of spectra reaches this cosine similarity.",
    ),
    (
        "condition_review_threshold",
        "Condition number requiring review",
        "Flags a reference matrix whose condition number reaches this value.",
    ),
    (
        "condition_fail_threshold",
        "Maximum condition number",
        "Rejects the reference matrix when its condition number reaches this value.",
    ),
)
BACKGROUND = (
    (
        "per_band_absolute_tolerance",
        "Channel background difference",
        "Allowed difference between reference and control background in normalized intensity units.",
    ),
    (
        "per_band_relative_tolerance",
        "Relative channel background difference",
        "Allowed background difference as a fraction of the channel signal.",
    ),
    (
        "l2_absolute_tolerance",
        "Overall background difference",
        "Allowed magnitude of the difference across all background channels.",
    ),
    (
        "l2_relative_tolerance",
        "Relative overall background difference",
        "Allowed overall difference relative to the background signal.",
    ),
    (
        "scale_floor",
        "Background comparison floor",
        "Small positive value used to stabilize ratios when the background is close to zero.",
    ),
    (
        "maximum_relative_roi_heterogeneity",
        "Variation between background regions",
        "Maximum relative variation allowed when reusing a shared background offset.",
    ),
)
PURIFICATION = (
    (
        "af_minimum_l2_norm",
        "Minimum autofluorescence signal",
        "Minimum L2 magnitude after background subtraction and clipping. Each control region and the combined control must exceed this value.",
    ),
    (
        "purified_minimum_l2_norm",
        "Minimum extracted fluorophore signal",
        "Minimum L2 magnitude after AF subtraction and clipping. The extracted fluorophore spectrum must exceed this value.",
    ),
)
ESTIMATION = (
    (
        "minimum_l2_norm",
        "Minimum autofluorescence signal",
        "Minimum combined signal required for the autofluorescence spectrum.",
    ),
)


# ==============================================================================
#                               Reference settings
# ==============================================================================


def reference_settings(action: str, checkpoint: str, measurement_count: int):
    controls, paths, groups, constants = [], {}, {}, {}

    def add(path, label, help_text, group, kind=ControlKind.NUMBER, value=None, required=True):
        name = f"reference_setting_{len(controls)}"
        controls.append(
            FormControl(name, label, kind, value=value, help_text=help_text, required=required)
        )
        paths[name] = tuple(path)
        groups[name] = group

    def policy(root, rows, group):
        for key, label, help_text in rows:
            add((root, key), label, help_text, group)

    def sampling(path=("sampling_policy",), structured=False, group="Reference pixel selection", initial=None):
        initial = initial or {}
        chunks = initial.get('processing_chunk_shape', (None, None))
        shape = (*path, "processing_chunk_shape")
        add(
            (*shape, "width" if structured else 0),
            "Processing block width (pixels)",
            "Width of the pixel block read at one time. Smaller blocks use less memory.",
            group,
            ControlKind.INTEGER,
            value=chunks[0],
        )
        add(
            (*shape, "height" if structured else 1),
            "Processing block height (pixels)",
            "Height of the pixel block read at one time. Smaller blocks use less memory.",
            group,
            ControlKind.INTEGER,
            value=chunks[1],
        )
        add(
            (*path, "saturation_fraction"),
            "Saturation cutoff (fraction)",
            "Excludes a pixel when any channel reaches this fraction of its recorded maximum.",
            group,
            value=initial.get('saturation_fraction'),
        )
        add(
            (*path, "near_zero_l2_threshold"),
            "Minimum spectral signal",
            "Excludes pixels whose corrected band intensities have an L2 magnitude at or below this value.",
            group,
            value=initial.get('near_zero_l2_threshold'),
        )

    if action in {"prepare_shared_control", "prepare_stained_reference"}:
        from mif_pipeline._defaults import default_section

        # New source-route starts: the block size is computational; 1/0 add
        # neither a near-saturation margin nor a positive signal cutoff.
        initial = default_section('unmixing.reference_preparation')
        sampling_initial = {**initial['sampling_policy'],
            'processing_chunk_shape': default_section('unmixing')['chunk_size']}
        role = 'control' if action == 'prepare_shared_control' else 'positive'
        sampling((role + '_sampling_policy',), group='Control pixels' if role == 'control' else 'Stained tissue pixels', initial=sampling_initial)
        sampling(('background_sampling_policy',), group='Background pixels', initial=sampling_initial)
        # Sharing is inapplicable on these routes (compatibility=None). The
        # positive scale floor is solely a numerical diagnostic convention.
        # Exact retained/source policies replace these constants on projection.
        constants['background_policy'] = initial['background_policy']
    elif action == "select_reference_regions":
        for role, title in (('positive', 'Stained tissue pixels'), ('control', 'Control pixels'),
                            ('background', 'Background pixels')):
            sampling((role + '_sampling_policy',), group=title)
        policy('background_policy', BACKGROUND, 'Background comparison')
    elif action == "advance_reference_purification":
        if checkpoint == "signal_dark_measurements":
            from mif_pipeline._defaults import default_section

            # The denominator guard and diagnostic ratio floor are computational
            # conventions. Keep all four values explicit in the recorded policy.
            initial = default_section("unmixing.reference_purification")
            constants["purification_policy"] = initial
            for key, label, help_text in PURIFICATION:
                add(("purification_policy", key), label, help_text,
                    "Fluorophore extraction", value=initial[key], required=False)
        elif checkpoint in {
            "positive_reference_rois",
            "af_reference_rois",
            "negative_reference_rois",
            "background_reference_rois",
        }:
            sampling()
            if checkpoint == "background_reference_rois":
                policy("background_policy", BACKGROUND, "Background comparison")
    elif action == "accept_live_consensus":
        from mif_pipeline._defaults import default_section
        from mif_pipeline.preprocessing.unmixing.reference_defaults import consensus_matrix_review_defaults

        # Current authoring uses fixed review criteria. These remain explicit in
        # the submitted policy; saved policies and their backend API stay intact.
        constants["consensus_policy"] = default_section("unmixing.reference_consensus")
        constants["qc_policy"] = consensus_matrix_review_defaults()
    elif action == "propose_control_target_af":
        sampling(structured=True)
        policy("background_policy", BACKGROUND, "Background comparison")
        policy("estimation_policy", ESTIMATION, "Autofluorescence estimation")
        policy("qc_policy", QC, "Reference separation checks")
    return tuple(controls), paths, groups, constants


def assemble_settings(values, paths, constants):
    result = _json_editable_value(constants)
    for name, path in paths.items():
        if name not in values:
            if path[0] in {"baseline_policy", "purification_policy"}:
                value = None
            else:
                raise ValueError(f"{name} is required")
        else:
            value = values[name]
        if value is None and path[0] == "purification_policy":
            # Clearing an optional override keeps its initialized policy value.
            continue
        current = result
        for index, key in enumerate(path):
            last = index == len(path) - 1
            if isinstance(current, list):
                while len(current) <= key:
                    current.append(None)
                if last:
                    current[key] = value
                else:
                    if current[key] is None:
                        current[key] = [] if isinstance(path[index + 1], int) else {}
                    current = current[key]
            elif last:
                current[key] = value
            else:
                current = current.setdefault(key, [] if isinstance(path[index + 1], int) else {})
    if "acceptance_policy" in result and not result["acceptance_policy"].get(
        "baseline_improvement_enabled", False
    ):
        result["baseline_policy"] = None
    elif "baseline_policy" in result and any(
        value is None for value in result["baseline_policy"].values()
    ):
        raise ValueError("Complete the fixed-reference comparison settings.")
    return result
