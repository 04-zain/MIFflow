"""Exact saved intensity decisions for measurements within named populations.

These selectors measure existing phenotype assignments; they never assign or
change a phenotype. Threshold review owns the accepted source references and
resolves pooled or image decisions into explicit image/section entries.
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Population marker selection contains duplicate JSON fields")
        result[key] = value
    return result


def parse_population_phenotype_ids(value: str) -> tuple[str, ...]:
    """Decode a nonempty distinct union of exact saved phenotype labels."""
    try:
        labels = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("Population phenotype_ids must be a JSON array") from exc
    if (not isinstance(labels, list) or not labels
            or any(not isinstance(label, str) or not label.strip() or label == "Not phenotyped" for label in labels)
            or len(set(labels)) != len(labels)):
        raise ValueError("Population phenotype_ids must contain distinct saved phenotype labels")
    return tuple(labels)


def parse_population_marker_gate(value: str | Mapping, measurement_id: str | None = None) -> dict:
    """Validate an intensity gate, rejecting scores and ambiguous section scope."""
    try:
        gate = json.loads(value, object_pairs_hook=_strict_object) if isinstance(value, str) else dict(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("Population marker gate must contain a saved intensity decision") from exc
    if (not isinstance(gate, dict) or set(gate) != {
            "schema_version", "measurement_id", "decisions", "source_ref_sha256s"}
            or gate["schema_version"] != "population-marker-gate/v1"
            or not isinstance(gate["measurement_id"], str) or not gate["measurement_id"].strip()
            or (measurement_id is not None and gate["measurement_id"] != measurement_id)):
        raise ValueError("Population marker gate must match its exact saved measurement")
    sources = gate["source_ref_sha256s"]
    if (not isinstance(sources, list) or not sources
            or any(not isinstance(item, str) or len(item) != 64
                   or any(char not in "0123456789abcdef" for char in item) for item in sources)
            or len(set(sources)) != len(sources)):
        raise ValueError("Population marker gate requires distinct accepted source references")
    decisions = gate["decisions"]
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("Population marker gate requires image and section decisions")
    scopes = set()
    for decision in decisions:
        if (not isinstance(decision, dict) or set(decision) != {
                "slide_id", "section_id", "threshold", "outcome", "intensity_domain", "method"}
                or any(not isinstance(decision[field], str) or not decision[field].strip()
                       for field in ("slide_id", "section_id"))
                or decision["outcome"] not in {"threshold", "all_positive", "all_negative"}
                or decision["intensity_domain"] not in {"finite", "nonnegative"}
                or decision["method"] not in {"manual", "triangle", "gmm", "gamma"}):
            raise ValueError("Population marker gate requires a reviewed intensity decision")
        cutoff = decision["threshold"]
        if decision["outcome"] == "threshold":
            if isinstance(cutoff, bool) or not isinstance(cutoff, (int, float)) or not math.isfinite(cutoff):
                raise ValueError("Population marker threshold must be finite")
        elif cutoff is not None:
            raise ValueError("Population marker constant decision cannot contain a threshold")
        scope = (decision["slide_id"], decision["section_id"])
        if scope in scopes:
            raise ValueError("Population marker gate repeats an image/section decision")
        scopes.add(scope)
    return gate


def marker_rule_for_section(gate: str | Mapping, slide_id: str, section_id: str):
    """Return the existing marker evaluator's rule for the exact saved scope."""
    from mif_pipeline.cell_analysis.marker_rules import ThresholdRule

    gate = parse_population_marker_gate(gate)
    matches = [item for item in gate["decisions"]
               if (item["slide_id"], item["section_id"]) == (slide_id, section_id)]
    if not matches:
        raise ValueError("Population marker gate does not cover this image and section")
    decision = matches[0]
    return ThresholdRule(
        biomarker=gate["measurement_id"], measurement=gate["measurement_id"],
        compartment="saved measurement", polarity="above", cutoff=decision["threshold"],
        confirmed=True, outcome=decision["outcome"], intensity_domain=decision["intensity_domain"],
    )
