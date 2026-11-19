"""Bounded pre-fit distributions from the accepted measurement stream.

Two passes retain feature extrema and fixed histogram bins, never cell vectors or
fitted models. The complete authenticated stream still establishes its recorded
cell inventory and domain membership before any preview becomes visible.
"""
from __future__ import annotations

import json

import numpy as np

from mif_pipeline.cell_analysis.measurement_plan import FeatureKind
from mif_pipeline.cell_analysis.measurement_rows import (
    MarkerIntensityRowsDescriptor,
    MeasurementRowStreamExpectation,
    validate_measurement_row_stream,
)
from mif_pipeline.cell_analysis.phenotyping_population import resolve_population_selection, population_includes
from ._common import BatchPhenotypingError


def _scan(measurement, callback, population_settings=None, expected_sources=None):
    features = tuple(item for item in measurement.measurement_plan.features
                     if item.kind == FeatureKind.EXPRESSION.value)
    reference = measurement.measurement_rows
    selection = resolve_population_selection(population_settings, sample_id=measurement.sample_id,
        project_entry_id=measurement.project_entry_id, slide_id=measurement.slide_id)
    stream = validate_measurement_row_stream(
        descriptor=MarkerIntensityRowsDescriptor(path=reference.path, content_schema_version=reference.content_schema_version, sha256=reference.sha256,
            byte_count=reference.byte_count, row_count=reference.row_count),
        authenticated_output_root=reference.path.parent.parent.parent,
        expected=MeasurementRowStreamExpectation(project_entry_id=measurement.project_entry_id,
            slide_id=measurement.slide_id, section_id=measurement.section_id,
            segmentation_generation_id=measurement.measurement_generation_id,
            measurement_generation_id=measurement.measurement_generation_id,
            segmentation_population_id=measurement.segmentation_population_id,
            analysis_domain_bindings=tuple({"analysis_domain_id": item.analysis_domain_id,
                "analysis_domain_revision_sha256": item.analysis_domain_revision_sha256}
                for item in measurement.analysis_domain_bindings),
            feature_ids=tuple(item.feature_id for item in features),
            measurement_names=tuple(item.measurement for item in features)),
        population_selection=selection,
        expected_population_source_ref_sha256s=(frozenset(expected_sources)
            if expected_sources is not None and selection['kind'] != 'all_segmented' else None),
        provisional_population_callback=lambda cell, domain, values, membership: callback(cell, domain, values, population_includes(selection, membership)))
    if (stream.cell_count != measurement.cell_count
            or stream.cell_ids_sha256 != measurement.cell_ids_sha256
            or stream.cell_to_domain_membership_sha256 != measurement.cell_to_domain_membership_sha256
            or stream.domain_cell_commitments_sha256 != measurement.domain_cell_commitments_sha256
            or stream.row_inventory_sha256 != reference.row_inventory_sha256):
        raise BatchPhenotypingError("marker preview rows differ from their accepted measurement population")
    return stream


def marker_distribution_previews(measurements, marker_id: str, *, bins: int = 128, population_settings=None, source_refs_by_measurement=None, population_options_out=None) -> tuple[dict[str, object], ...]:
    """Describe every offered feature for one marker, pooled and by physical image.

    Each committed measurement descriptor retains its own publication directory,
    including earlier sections accepted before an analysis import.

    The preview uses all finite included measurements. Missing/excluded counts are
    separate from histogram totals; the default current population excludes no
    accepted rows. No sampling, numerical fit or threshold selection occurs here.
    """
    if not 4 <= bins <= 256:
        raise BatchPhenotypingError("marker preview bin count is outside its bounded range")
    features = tuple(item for item in measurements[0].measurement_plan.features
                     if item.kind == FeatureKind.EXPRESSION.value)
    selected = tuple((index, item) for index, item in enumerate(features) if item.biomarker == marker_id)
    if not selected:
        raise BatchPhenotypingError("marker has no offered measured feature")
    scopes = tuple(sorted({(item.sample_id, item.project_entry_id, item.slide_id) for item in measurements}))
    # Each state is small and bounded by offered features, images and fixed bins.
    states = {(feature.feature_id, scope): {"count": 0, "unknown": 0, "excluded": 0, "low": float("inf"), "high": -float("inf")}
              for _, feature in selected for scope in (None, *scopes)}
    options = {}
    for measurement in measurements:
        scope = (measurement.sample_id, measurement.project_entry_id, measurement.slide_id)
        def extrema(_cell_id, _domain_id, values, included):
            for index, feature in selected:
                value = float(values[index]) if values[index] is not None else float("nan")
                for key in (None, scope):
                    state = states[(feature.feature_id, key)]
                    if not included:
                        state["excluded"] += 1
                    elif not np.isfinite(value):
                        state["unknown"] += 1
                    else:
                        state["count"] += 1
                        state["low"] = min(state["low"], value)
                        state["high"] = max(state["high"], value)
        stream = _scan(measurement, extrema, population_settings,
            None if source_refs_by_measurement is None else source_refs_by_measurement[measurement.artifact_ref.artifact_ref_sha256])
        header = stream.header
        kinds = {"all_segmented"}
        classes = {"primary_structure": set(), "secondary_structure": set()}
        if header.population_sources is not None:
            kinds.add("detected_tissue")
            # Secondary structures are produced after the phenotype result;
            # old measured memberships must not offer a circular population.
            for kind in ("primary_structure",):
                by_domain = [{class_id for source in header.population_sources
                    if source["kind"] == kind and source["analysis_domain_id"] == domain
                    for class_id in source["class_ids"]} for domain in header.analysis_domain_ids]
                classes[kind] = set.intersection(*by_domain) if by_domain else set()
                if classes[kind]:
                    kinds.add(kind)
        if scope in options:
            prior = options[scope]
            kinds &= prior["kinds"]
            classes = {key: values & prior["classes"][key] for key, values in classes.items()}
        options[scope] = {"kinds": kinds, "classes": classes}
    for state in states.values():
        extent = [state["low"], state["high"]] if state["count"] else [0., 1.]
        state["edges"] = np.histogram_bin_edges(extent, bins=bins)
        state["counts"] = np.zeros(bins, dtype=np.int64)
    for measurement in measurements:
        scope = (measurement.sample_id, measurement.project_entry_id, measurement.slide_id)
        def count(_cell_id, _domain_id, values, included):
            if not included:
                return
            for index, feature in selected:
                value = float(values[index]) if values[index] is not None else float("nan")
                if not np.isfinite(value):
                    continue
                for key in (None, scope):
                    state = states[(feature.feature_id, key)]
                    bin_index = min(bins - 1, max(0, int(np.searchsorted(state["edges"], value, side="right")) - 1))
                    state["counts"][bin_index] += 1
        _scan(measurement, count, population_settings,
            None if source_refs_by_measurement is None else source_refs_by_measurement[measurement.artifact_ref.artifact_ref_sha256])
    def projection(state):
        if int(state["counts"].sum()) != state["count"]:
            raise BatchPhenotypingError("marker preview histogram lost finite measurements")
        return {"histogram": {"counts": state["counts"].tolist(), "bin_edges": state["edges"].tolist()},
            "eligible_count": state["count"], "unknown_count": state["unknown"], "excluded_count": state["excluded"]}
    if population_options_out is not None:
        population_options_out.update(default=population_settings, slides=tuple({
            "image_identity": json.dumps(scope, separators=(",", ":"), ensure_ascii=False),
            "sample_id": scope[0], "project_entry_id": scope[1], "slide_id": scope[2],
            "available_kinds": sorted(options[scope]["kinds"]),
            "primary_classes": [{"class_id": item, "label": item} for item in sorted(options[scope]["classes"]["primary_structure"])],
            "secondary_classes": [{"class_id": item, "label": item} for item in sorted(options[scope]["classes"]["secondary_structure"])]
        } for scope in scopes))
    return tuple({"marker_id": marker_id, "feature_id": feature.feature_id,
        "measurement": feature.measurement, "compartment": feature.compartment,
        "pooled": projection(states[(feature.feature_id, None)]),
        "per_slide": tuple({"image_identity": json.dumps(scope, separators=(",", ":"), ensure_ascii=False),
            "sample_id": scope[0], "project_entry_id": scope[1], "slide_id": scope[2],
            **projection(states[(feature.feature_id, scope)])} for scope in scopes)} for _, feature in selected)
