"""Shared criterion names and values for structure authoring and result review."""
from __future__ import annotations

import re
import json

from .terminology import cell_measurement_label

# Labels and producer units; the semantic parser remains the schema authority.
QUANTITIES = {
    "segmented_cell_count": ("Number of cells", "cells"),
    "segmented_nucleus_count": ("Number of nuclei", "nuclei"),
    "phenotype_count": ("Number of cells of a phenotype", "cells"),
    "phenotype_fraction": ("Fraction of cells of a phenotype", "fraction"),
    "marker_state_count": ("Number of marker-positive or marker-negative cells", "cells"),
    "marker_state_fraction": ("Fraction of marker-positive or marker-negative cells", "fraction"),
    "population_marker_fraction": ("Marker fraction within a phenotype population", "fraction"),
    "cell_density": ("Cell density", "cells/mm²"),
    "nucleus_density": ("Nucleus density", "nuclei/mm²"),
    "phenotype_density": ("Density of cells of a phenotype", "cells/mm²"),
    "cell_measurement_aggregate": ("Cell measurement", "measurement units"),
    "distance_to_primary": ("Distance to a primary compartment", "µm"),
    "overlap_fraction_with_primary": ("Overlap with a primary compartment", "fraction"),
    "candidate_area": ("Region area", "µm²"),
    "geometry_component_count": ("Number of disconnected parts", "parts"),
    "classifier_confidence": ("Classifier confidence", "score"),
}
SELECTORS = {
    "phenotype_count": ("phenotype_id",),
    "phenotype_fraction": ("phenotype_id", "denominator"),
    "phenotype_density": ("phenotype_id",),
    "marker_state_count": ("marker_id", "marker_state_id"),
    "marker_state_fraction": ("marker_id", "marker_state_id", "denominator"),
    "population_marker_fraction": ("phenotype_ids", "measurement_id", "marker_state_id", "denominator", "missing_policy", "positivity_gate"),
    "cell_measurement_aggregate": ("measurement_id", "statistic", "missing_policy"),
    "distance_to_primary": ("primary_class_id",),
    "overlap_fraction_with_primary": ("primary_class_id", "denominator"),
}
SELECTOR_LABELS = {
    "phenotype_ids": "Population", "positivity_gate": "Positivity threshold",
    "phenotype_id": "Phenotype", "marker_id": "Marker", "marker_state_id": "Marker state",
    "measurement_id": "Measurement", "channel_role": "Channel", "statistic": "Across cells",
    "missing_policy": "Missing values", "primary_class_id": "Primary compartment", "denominator": "Divide by",
}
COMPARISONS = (("At least (≥)", "ge"), ("More than (>)", "gt"), ("At most (≤)", "le"),
               ("Less than (<)", "lt"), ("Equal to (=)", "eq"))


def measurement_column_label(measurement_id):
    """Caption known intensity columns; retain other supplied measurement names.

    The column identity remains the stored QuPath key. Only recognised intensity
    quantities and cell compartments are expanded for display.
    """
    name = str(measurement_id)
    match = re.fullmatch(r"(Cell|Nucleus|Cytoplasm|Membrane): (.+): (Mean|Median|Min|Max|Std\.Dev\.|Variance)", name)
    if match is None:
        return name
    compartment, marker, quantity = match.groups()
    return marker + " · " + cell_measurement_label(compartment, quantity).lower()


def criterion_review_values(item):
    """Present the recorded outcome without recalculating the scientific decision."""
    kind = item.get("kind", "")
    selector = item.get("selector") or {}
    label = item.get("prompt") if kind == "manual_region_decision" else None
    label = label or QUANTITIES.get(kind, (str(kind).replace("_", " "), ""))[0]
    selected = [str(selector[key]) for key in
                ("phenotype_id", "marker_id", "primary_class_id", "measurement_id")
                if selector.get(key)]
    if selector.get("marker_state_id"):
        selected.append(str(selector["marker_state_id"]))
    if kind == "cell_measurement_aggregate" and selector.get("measurement_id"):
        label = measurement_column_label(selector["measurement_id"])
    elif selected:
        label += ": " + "; ".join(selected)
    population_name = "cells"
    if kind == "population_marker_fraction":
        try:
            names = json.loads(selector.get("phenotype_ids", "[]"))
        except (TypeError, ValueError):
            names = []
        population_name = " + ".join(names) if names else "cells"
        if not population_name.lower().endswith((" cells", " cell")) and population_name != "cells":
            population_name += " cells"
        label = measurement_column_label(selector.get("measurement_id", "")) + " · " + selector.get("marker_state_id", "positive") + " fraction in " + " + ".join(names)
    if item.get("semantic_class_id") or item.get("subtype_id"):
        label = str(item.get("semantic_class_id") or item["subtype_id"]) + " — " + label
    unit = item.get("unit") or QUANTITIES.get(kind, ("", ""))[1]
    if unit in {"fraction", "score", "measurement units"}:
        unit = ""

    def value_text(value):
        if value is None:
            return "—"
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if isinstance(value, (int, float)):
            return f"{value:,.6g}" + (f" {unit}" if unit else "")
        return str(value)

    comparator = {"ge": "≥", "gt": ">", "le": "≤", "lt": "<", "eq": "="}.get(item.get("comparator"), "")
    condition = (comparator + " " + value_text(item["threshold"])).strip() if item.get("threshold") is not None else ("Answer the question" if kind == "manual_region_decision" else "—")
    if item.get("required") is False:
        condition += "; Optional"
    outcome = item.get("outcome", item.get("status"))
    result = {"pass": "Met", "fail": "Not met", "available": "Measured", "unavailable": "Unavailable",
              "needs_review": "Needs an answer"}.get(outcome, str(outcome or ""))
    inputs = item.get("calculation_inputs") or {}
    details = []
    count = inputs.get("selected_cell_count")
    members = inputs.get("member_cell_count")
    area = inputs.get("area_um2")
    if count is not None and members is not None:
        details.append(f"{count} of {members} cells")
    if count is not None and area is not None:
        population = "nuclei" if kind == "nucleus_density" else "cells"
        details.append(f"{count} {population} in {area / 1_000_000:.6g} mm²")
    if selector.get("statistic"):
        details.append(str(selector["statistic"]).capitalize() + " across member cells")
    if selector.get("denominator") in {"candidate_area", "primary_area"}:
        details.append("Divided by " + {"candidate_area": "this region's area",
                                      "primary_area": "the primary compartment's total area"}[selector["denominator"]])
    if outcome == "unavailable" and item.get("unavailable_reason"):
        reason = item["unavailable_reason"]
        details.append({
            "measurement_source_absent": "The selected measurement is missing for one or more cells, or this region has no cells.",
            "phenotyping_population_does_not_cover_candidate": "Some cells in this region have not been phenotyped.",
            "segmented_cell_denominator_is_zero": "This region contains no cells to use as the denominator.",
            "calibrated_candidate_area_unavailable": "The region's area in physical units is unavailable.",
            "candidate_area_source_absent": "The region's area is unavailable.",
            "primary_geometry_source_absent": "Outlines for the selected primary compartment are unavailable.",
            "primary_overlap_source_absent": "Overlap with the selected primary compartment is unavailable.",
            "classifier_confidence_source_absent": "The classifier did not record a score for this region.",
            "selected_generation_mismatch": "The requested phenotype result differs from the result being reviewed.",
        }.get(reason, str(reason).replace("_", " ")))
    measured = value_text(item.get("value"))
    if kind == "population_marker_fraction":
        denominator = inputs.get("valid_marker_cell_count")
        if count is not None and denominator is not None:
            measured = (f"{count:,} {population_name} / {denominator:,} cells in the region"
                        if selector.get("denominator") == "all_valid_marker_cells" else
                        f"{count:,}/{denominator:,} {population_name}")
            if item.get("value") is not None:
                measured += f", {item["value"]:.1%}"
        excluded = inputs.get("denominator_excluded_cell_count", inputs.get("unknown_cell_count", 0))
        if excluded:
            details.append(f"{excluded:,} {"cell" if excluded == 1 else "cells"} excluded")
    elif kind == "cell_measurement_aggregate" and inputs.get("excluded_cell_count"):
        details.append(f"{inputs["excluded_cell_count"]:,} cells excluded because the measurement is unavailable")
    return label, measured, condition, result, "; ".join(details)
