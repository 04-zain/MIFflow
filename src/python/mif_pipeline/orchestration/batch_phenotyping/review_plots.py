"""Calculate and retain an optional UMAP view of accepted clustering measurements.

Only the selected numerical columns enter this operation. The saved coordinate
sample preserves its exact cell identities and measured values, so colour changes
and later visits reuse the fit without loading geometry or refitting clusters.
"""
from __future__ import annotations

from dataclasses import asdict
from importlib.metadata import version
import io
import json
import uuid

import numpy as np

from mif_pipeline.cell_analysis.embeddings import EmbeddingArtifact, build_umap_artifact
from mif_pipeline.cell_analysis.embedding_plots import shade_embedding
from ._common import digest
from .execution import _assemble_selected_matrix
from .review_execution import _validate_sources


def view_key(evidence, policy):
    """Identify the same accepted population and fit across application sessions."""
    return digest({"clustering": evidence.clustering_result_ref_sha256,
                   "sources": evidence.source_authority_sha256s,
                   "policy": asdict(policy), "umap": version("umap-learn")})


def prepare_view(evidence, snapshot, policy):
    """Reopen the current accepted inputs before consulting the coordinate cache."""
    authorities = {item.artifact_ref.artifact_ref_sha256: item
                   for item in snapshot.committed_artifact_authorities}
    clustering = authorities[evidence.clustering_result_ref_sha256]
    sources = tuple(authorities[key] for key in evidence.source_authority_sha256s)
    result, resolved, features = _validate_sources(
        clustering_authority=clustering, source_authorities=sources,
        maximum_clustering_result_byte_count=max(item.artifact_ref.byte_count or 0 for item in (clustering, *sources)),
    )
    key = view_key(evidence, policy)
    return key, result, resolved, features


def memory_allowances(count, feature_count, policy):
    """Separate complete-population preparation from the sampled UMAP workspace.

    These are conservative reservations, not a measured or enforced peak limit.
    Lowering max_cells only reduces the second allowance.
    """
    population = 32 * count * feature_count + 512 * count
    sample = min(count, policy.max_cells)
    workspace = 1024**3 + 256 * sample * min(policy.neighbors, max(1, sample - 1))
    return population, workspace


def cache_path(summaries, key):
    """Locate the derived view beneath the existing analysis summaries folder."""
    directory = summaries / "umap"
    if directory.is_symlink():
        raise ValueError("The UMAP cache folder must be inside this analysis.")
    directory.mkdir(exist_ok=True)
    return directory / (key + ".parquet")


def read_view(path, key, features):
    """Reuse valid coordinates, or request recalculation of a damaged derived cache.

    Permission and storage errors remain actionable errors. Invalid Parquet or
    metadata never changes scientific inputs and can be replaced by the next
    successful calculation, without asking the researcher to edit files.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError("The saved UMAP view must be inside this analysis.")
    try:
        return _decode_view(pq.read_table(path), key, features)
    except (pa.ArrowInvalid, ValueError, KeyError, TypeError):
        return None


def _decode_view(table, key, features):
    """Validate source identity and the complete sampled values before reuse."""
    metadata = json.loads(table.schema.metadata[b"mif:umap"])
    if metadata["key"] != key:
        raise ValueError("The saved UMAP view belongs to different inputs or settings.")
    rows = table.to_pydict()
    view = EmbeddingArtifact(
        status="available", reason=None, message=None,
        cell_ids=tuple(rows["cell_identity"]),
        coordinates=tuple(zip(rows["umap_1"], rows["umap_2"])), feature_specs=features,
        feature_values={spec.feature_id or spec.measurement: tuple(rows[f"feature_{i}"])
                        for i, spec in enumerate(features)},
        categorical_values={name: tuple(rows[name]) for name in
                            ("cluster_id", "section_id", "primary_compartment", "display_label")},
        parameters=metadata["parameters"], backend_version=metadata["backend_version"],
    )
    if view.content_hash != metadata["content_hash"]:
        raise ValueError("The saved UMAP coordinate content differs.")
    return view


def calculate_view(result, resolved, features, policy):
    """Read accepted rows once, preserving composite identity and assignment order."""
    decoded = tuple((preparation, preparation.measurement_result_ref_sha256)
                    for preparation, _ in resolved)
    measurements = {preparation.measurement_result_ref_sha256: measurement
                    for preparation, measurement in resolved}
    values, identities, _, _, _ = _assemble_selected_matrix(
        decoded, measurements, features, len(result.assignments))
    assignments = {(row.sample_id, row.project_entry_id, row.cell_id): row
                   for row in result.assignments}
    ordered = [assignments[(row.sample_id, row.project_entry_id, row.cell_id)] for row in identities]
    encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    ids = tuple(encode((row.sample_id, row.project_entry_id, row.cell_id)) for row in identities)
    # Section labels include the image identity; repeated section names in two
    # images must remain separate sampling groups. No compartment is invented.
    categories = {
        "cluster_id": tuple(row.cluster_id for row in ordered),
        "section_id": tuple(encode((row.sample_id, row.project_entry_id, row.section_id)) for row in ordered),
        "primary_compartment": ("Unknown",) * len(ordered),
    }
    view = build_umap_artifact(values, features, cell_ids=ids, categories=categories, policy=policy)
    if not view.is_available:
        raise ValueError(view.message or "UMAP could not calculate this view. Adjust its settings and retry.")
    return view


def save_view(path, key, view):
    """Publish one compressed sampled table after the complete fit succeeds."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    rows = {"cell_identity": view.cell_ids,
            "umap_1": [point[0] for point in view.coordinates],
            "umap_2": [point[1] for point in view.coordinates],
            **view.categorical_values,
            **{f"feature_{i}": view.feature_values[spec.feature_id or spec.measurement]
               for i, spec in enumerate(view.feature_specs)}}
    metadata = {"key": key, "parameters": dict(view.parameters),
                "backend_version": view.backend_version, "content_hash": view.content_hash}
    # The existing artifact export supplies JSON-compatible parameter sequences.
    metadata["parameters"] = view.to_export_mapping()["parameters"]
    table = pa.table(rows).replace_schema_metadata({b"mif:umap": json.dumps(metadata, allow_nan=False).encode()})
    temporary = path.with_suffix("." + uuid.uuid4().hex + ".part")
    try:
        pq.write_table(table, temporary, compression="zstd")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def render_view(view, attribute="cluster_id"):
    """Render saved coordinates and export their aligned identities and colour values."""
    from mif_pipeline.cell_analysis.result_plots import _csv_output
    from matplotlib.figure import Figure
    from matplotlib import rc_context
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    plot = shade_embedding(view, attribute, max_display_points=len(view.cell_ids))
    figure = Figure(figsize=(8, 6), constrained_layout=True)
    FigureCanvasAgg(figure)
    axes = figure.add_subplot()
    points = np.asarray(plot.coordinates)
    if attribute in view.feature_values:
        artist = axes.scatter(points[:, 0], points[:, 1], c=plot.colors,
                              s=9, alpha=.65, linewidths=0, cmap="viridis", rasterized=True)
        spec = next(spec for spec in view.feature_specs
                    if (spec.feature_id or spec.measurement) == attribute)
        title = spec.measurement
        figure.colorbar(artist, ax=axes, label=title + " (" + (spec.units or "measurement units") + ")")
    else:
        for label in sorted(set(plot.colors)):
            selected = np.asarray([value == label for value in plot.colors])
            axes.scatter(points[selected, 0], points[selected, 1], s=9, alpha=.65,
                         linewidths=0, label=label, rasterized=True,
                         color=plot.color_metadata["category_to_color"][label])
        axes.legend(loc="center left", bbox_to_anchor=(1, .5), frameon=False, markerscale=2)
        title = "Clusters"
    axes.set(xlabel="UMAP 1", ylabel="UMAP 2")
    axes.set_aspect('equal',adjustable='box')
    axes.spines[["top", "right"]].set_visible(False)
    outputs = []
    try:
        from mif_pipeline.cell_analysis.figure_style import apply_figure_font
        apply_figure_font(figure)
        with rc_context({"svg.fonttype": "none", "pdf.fonttype": 42}):
            for image_format in ("png", "pdf", "svg"):
                output = io.BytesIO()
                figure.savefig(output, format=image_format, dpi=300, facecolor="white")
                outputs.append((plot.plot_id + "/" + image_format,
                                "UMAP: " + title, image_format, output.getvalue()))
        metadata = plot.color_metadata
        outputs.append(_csv_output(plot.plot_id, "UMAP coordinates and colour data",
            ("sample_id", "project_entry_id", "cell_id", "cluster_id", "umap_1", "umap_2",
             "colour_attribute", "colour_value", "measurement", "units", "colour_kind",
             "colour_map", "colour_min", "colour_max", "category_colour"),
            ((*json.loads(identity), cluster, x, y, attribute, colour,
              metadata.get("measurement"), metadata.get("units"), metadata["kind"],
              metadata.get("colormap"), metadata.get("vmin"), metadata.get("vmax"),
              metadata.get("category_to_color", {}).get(colour))
             for identity, (x, y), colour, cluster in zip(plot.display_cell_ids, plot.coordinates,
                                               plot.colors, view.categorical_values['cluster_id'], strict=True))))
    finally:
        figure.clear()
    return tuple(outputs)
