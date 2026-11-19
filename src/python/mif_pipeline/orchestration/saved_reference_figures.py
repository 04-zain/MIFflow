"""Reopen the reference spectra used by an accepted prepared image.

The image result identifies the final library. We read that small saved record
directly, without reopening source pixels or the reference workflow's current
review, which may already describe a later library revision.
"""

from io import BytesIO
from pathlib import Path


def load_saved_reference_basis(operation_root: Path, identity):
    """Return the exact recorded library, or None for an image copied without unmixing."""
    from mif_pipeline.preprocessing.unmixing.reference_service_artifacts import (
        _MAX_ARTIFACT_BYTES, _parse_artifact, _strict_object,
    )
    from mif_pipeline.preprocessing.unmixing.reference_store_fs import _read_bounded_regular

    digest = identity.get("final_basis_sha256")
    if digest is None:
        return None
    if digest != identity.get("final_authority_sha256"):
        raise ValueError("The saved reference library does not match this prepared image.")
    paths = (operation_root / ".reference-artifacts").glob(
        f"*/final_basis_artifact-*-{digest}.json")
    for path in paths:
        raw = _read_bounded_regular(path, limit=_MAX_ARTIFACT_BYTES,
                                    label="saved reference spectra")
        envelope = _strict_object(raw)
        stored = _parse_artifact(
            raw, run_sha256=path.parent.name,
            start_authority_sha256=envelope["start_authority_sha256"],
            authority_checkpoint_sha256=envelope["authority_checkpoint_sha256"],
            kind="final_basis_artifact", subject_id="final-basis",
        )
        record = stored.record
        if (record.content_sha256 == digest
                and record.artifact_id == identity.get("final_authority_id")):
            return record
    raise ValueError("The reference spectra used for this prepared image are unavailable.")


def reference_operation_root(authority, snapshot):
    """Locate the exact producer, including its settled-review revision."""
    ref = authority.artifact_ref
    sample = next(item for item in snapshot.manifest.enabled_samples
                  if item.sample_id == ref.scope.sample_id)
    producer = next((node for node in snapshot.plan.nodes
                     if node.node_id == ref.producer_node_id), None)
    if producer is None:
        raise ValueError("The task that prepared this image is unavailable.")
    operation_root = sample.sample_output_root / "preprocessing" / "reference-unmixing"
    if producer.settled_review_revision is not None:
        operation_root = operation_root / "revisions" / producer.node_id
    return operation_root



def reference_basis(authority, snapshot):
    """Find the accepted image's library without opening its image data."""
    ref = authority.artifact_ref
    if ref.owning_identity.get("final_basis_sha256") is None:
        return None
    return load_saved_reference_basis(reference_operation_root(authority, snapshot), ref.owning_identity)


def render_reference_figures(basis):
    """Draw saved spectra and export both basis values and displayed signal fractions."""
    from mif_pipeline.cell_analysis.result_plots import _csv_output
    import numpy as np
    from matplotlib import rc_context
    from mif_pipeline.preprocessing.unmixing.spectral_plots import build_spectral_figure

    measurements = basis.proposal.axis.measurements
    wavelength_axis = bool(measurements) and all(item.wavelength is not None for item in measurements)
    unit = measurements[0].wavelength_unit if wavelength_axis else None
    x = ([item.wavelength for item in measurements] if wavelength_axis
         else np.arange(len(basis.proposal.axis.parent_band_ids)))
    profiles = []
    for column in basis.proposal.columns:
        values = np.asarray(column.values, dtype=float)
        profiles.append((x, values / values.sum(), column.component_identity, None))
    figure = build_spectral_figure(profiles,
        measurement_names=basis.proposal.axis.parent_band_ids, wavelength_unit=unit,
        optical_settings=(tuple(item.optical_setting for item in measurements)
                          if any(item.optical_setting is not None for item in measurements) else None))
    try:
        outputs = []
        with rc_context({"svg.fonttype": "none", "pdf.fonttype": 42}):
            for fmt in ("png", "pdf", "svg"):
                output = BytesIO()
                figure.savefig(output, format=fmt, dpi=300, facecolor="white", bbox_inches="tight")
                outputs.append(("reference-spectra/" + fmt, "Accepted reference spectra", fmt,
                                output.getvalue()))
        outputs.append(_csv_output("reference-spectra", "Accepted reference spectra · Data",
            ("component", "parent_band", "wavelength", "wavelength_unit", "saved_basis_value", "displayed_signal_fraction"),
            ((column.component_identity, band,
              measurements[index].wavelength if measurements else None,
              measurements[index].wavelength_unit if measurements else None, value, fraction)
             for column, profile in zip(basis.proposal.columns, profiles, strict=True)
             for index, (band, value, fraction) in enumerate(zip(basis.proposal.axis.parent_band_ids,
                 column.values, profile[1], strict=True)))))
        return tuple(outputs)
    finally:
        figure.clear()
