"""Researcher-facing spectral choices; persisted identifiers stay unchanged."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

SOURCE_CHOICES = {
    "accept_control_target_af": (
        "Use these spectral references",
        "Review the fluorophore and autofluorescence spectra, then start unmixing.",
    ),
    "retry_control_target_af": (
        "Prepare the control spectrum again",
        "Repeat autofluorescence extraction from the selected control.",
    ),
    "reselect_target_af_source": (
        "Change the autofluorescence reference",
        "Return to the autofluorescence source selection.",
    ),
    "accept_signal_dark_measurements": (
        "Use selected bands",
        "Choose the band that best shows the fluorophore. Use bands with negligible fluorophore signal to estimate how much of the control spectrum to subtract.",
    ),
    "accept_extraction_qc": ("Use included regions", "Inspect the subtraction for each region and choose which spectra to combine."),
    "retry_extraction": ("Repeat extraction review", "Repeat extraction review using the selected region decisions."),
    "reselect_reference_rois": ("Edit reference regions", "Return to region editing and repeat the dependent measurements."),
    "revise_signal_dark_measurements": ("Change extraction bands", "Change the signal band and the bands used to estimate autofluorescence."),
    "accept_fluorophore_consensus": (
        "Accept consensus spectrum",
        "Compare the regional spectra and choose which regions to include.",
    ),
    "shared_unstained_control": (
        "One shared unstained control",
        "Use one unstained control image when it represents the tissue autofluorescence in all single-stain references.",
    ),
    "per_stain_negative_controls": (
        "Separate controls for each stain",
        "Use a matched control for each single-stain reference when differences in tissue or preparation require separate autofluorescence estimates.",
    ),
    "curated_reference_library": (
        "Import or enter a spectral library",
        "Select a saved library or CSV file, or enter reference spectra in a table. Saved libraries retain their recorded normalization and imaging settings.",
    ),
    "reselect_basis_source": (
        "Change spectral references",
        "Return to the source choice. Previous choices are retained in the analysis history.",
    ),
    "accept_final_run_basis": (
        "Use these spectral references",
        "Fit these spectra to the image and save one channel per component.",
    ),
    "propose_final_basis": (
        "Check the spectral references",
        "Calculate spectral overlap and review the selected components.",
    ),
    "abort": ("Stop this step", "Retain the choices and results already saved."),
    "phenoimager_motif_profile": (
        "Use the PhenoImager MOTiF library",
        "Use the supplied spectra for the PhenoImager MOTiF imaging system. The library includes a tissue autofluorescence spectrum.",
    ),
    "import_reference_library": (
        "Import a spectral library",
        "Select a saved library (.miflib.zip or reviewed library JSON). Its channels and imaging settings will be checked against this image.",
    ),
    "construct_reference_library": (
        "Create a library from reference images",
        "Add single-stained reference images and their unstained or negative controls. Select reference regions to estimate each fluorophore spectrum.",
    ),
    "abort_workflow": (
        "Stop spectral unmixing",
        "Stop this step and retain the work already saved.",
    ),
}
CHECKPOINTS = {
    "control_strategy": (
        "Choose the reference controls",
        "The control images are used to estimate and subtract tissue autofluorescence when extracting each fluorophore’s reference spectrum from its single-stain image.",
    ),
    "basis_source": (
        "Choose spectral references",
        "Use a supplied library, import or enter spectral references, or create a library from reference images.",
    ),
    "reference_control_files": (
        "Add reference images",
        "Add the single-stained images and select their matching controls.",
    ),
    "positive_reference_rois": (
        "Select stained regions",
        "Choose regions containing the fluorophore signal in the reference image.",
    ),
    "af_reference_rois": (
        "Select autofluorescence regions",
        "Choose regions that represent tissue autofluorescence in the unstained control.",
    ),
    "negative_reference_rois": (
        "Match negative controls",
        "Choose negative-control regions matched to the stained reference.",
    ),
    "background_reference_rois": (
        "Select image background",
        "Choose background regions for the reference and control images.",
    ),
    "signal_dark_measurements": (
        "Choose bands for fluorophore extraction",
        "Choose the band that best shows the fluorophore. Use bands with negligible fluorophore signal to estimate how much of the control spectrum to subtract.",
    ),
    "fluorophore_consensus": (
        "Review the fluorophore spectrum",
        "Review the spectra from the selected regions and choose those to include.",
    ),
    "extraction_qc": ("Review fluorophore extraction", "Inspect the fluorescence remaining in each stained region after subtracting its estimated autofluorescence. Choose the regions to include in the fluorophore reference."),
    "target_af": (
        "Choose an autofluorescence reference",
        "Choose how tissue autofluorescence will be estimated for this image.",
    ),
    "target_af_control_review": (
        "Review spectral references",
        "Review the fluorophore and autofluorescence spectra before unmixing.",
    ),
    "final_run_basis": (
        "Review spectral references",
        "Review the fluorophore and autofluorescence spectra before unmixing.",
    ),
    "final_basis": (
        "Review spectral references",
        "Review the fluorophore and autofluorescence spectra before unmixing.",
    ),
}
FIELD_COPY = {
    "acquisition_confirmation": ("Matching imaging settings", ""),
    "normalization": (
        "Reference normalization",
        "Select the normalization already used by the supplied spectra.",
    ),
    "measurement_convention": (
        "Reference intensities before normalization",
        "Select the correction applied before the reference spectra were normalized.",
    ),
    "csv_locator": (
        "Spectral CSV file",
        "Rows are measurement channels and columns are fluorophore or autofluorescence components.",
    ),
    "library_locator": (
        "Spectral library file",
        "Select a .miflib.zip package, reviewed library JSON or CSV file, then review its spectral table.",
    ),
    "input_id": (
        "Reference name",
        "Enter a short name to identify this reference throughout the analysis.",
    ),
    "roi_export_locator": (
        "Selected reference regions",
        "Select the region file saved from the reference image in QuPath.",
    ),
    "positive_input_ids": (
        "Single-stained references",
        "Select the reference image for each fluorophore.",
    ),
    "negative_input_ids": (
        "Negative controls",
        "Select the negative controls matched to the stained references.",
    ),
    "shared_unstained_input_id": (
        "Unstained control",
        "Select the unstained image used with these references.",
    ),
    "selected_roi_ids": ("Reference regions", "Select the regions to include."),
    "signal_measurement_id": (
        "Signal channel",
        "Select the channel containing the reference fluorophore signal.",
    ),
    "dark_measurement_ids": (
        "Bands for estimating autofluorescence",
        "Choose bands with negligible fluorophore signal to estimate the autofluorescence contribution.",
    ),
    "reason_codes": ("Review notes", "Record the reason for this choice."),
}

# ==============================================================================
#                                 Reference copy
# ==============================================================================


def option_copy(requirement):
    return SOURCE_CHOICES.get(
        requirement["option_value"], (requirement["label"], requirement["description"])
    )


def checkpoint_copy(checkpoint):
    return CHECKPOINTS.get(
        checkpoint,
        ("Review spectral references", "Review the selected images, regions and spectra."),
    )


SPECTRAL_CHOICE_LABELS = {
    "normalization": {
        "l1": "L1: columns sum to one",
        "l2": "L2: columns have unit length",
    },
    "measurement_convention": {
        "acquisition_normalized": "Fully corrected intensities",
        "exposure_normalized": "Exposure-normalized intensities",
        "stored_calibrated": "Same corrections as the image",
        "target_raw_acquisition": "Raw intensities",
    },
}

REFERENCE_CORRECTION_HELP = {
    "target_raw_acquisition": (
        "Reference spectra must have been measured using the same imaging settings as the images in the processing group. "
        "We apply the processing group's intensity correction to these spectra."
    ),
    "acquisition_normalized": (
        "Exposure time, gain, binning and detector response have already been corrected. "
        "Use the same correction procedure as the image."
    ),
    "exposure_normalized": (
        "Intensities have already been divided by exposure time. References and batch "
        "images must share gain, binning, illumination and detector settings."
    ),
    "stored_calibrated": (
        "References and batch images must share imaging settings and correction "
        "procedures, including identical exposure times for each channel."
    ),
}
