"""Select the existing measured cells used for phenotyping.

A dataset selection may be replaced for one complete slide identity. Structure
selections are unions of accepted classifier regions; native measurement export
records whether each cell centroid is covered, including region boundaries.
Cells outside the selection remain part of the segmentation and its denominators.
"""
from __future__ import annotations

from collections.abc import Mapping
import json


class PhenotypingPopulationError(ValueError):
    """The selected regions are missing, stale, or do not describe this slide."""


KINDS = frozenset({"all_segmented", "detected_tissue", "primary_structure"})
NOT_PHENOTYPED = "Not phenotyped"


def slide_population_key(sample_id: str, project_entry_id: str, slide_id: str) -> str:
    """Keep slides with equal display names in different samples distinct."""
    values = (sample_id, project_entry_id, slide_id)
    if any(not isinstance(item, str) or not item or item != item.strip() for item in values):
        raise PhenotypingPopulationError("A population override requires the complete slide identity")
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _selection(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"kind", "class_ids"}:
        raise PhenotypingPopulationError("Choose the phenotyping region and its structure classes")
    kind = value["kind"]
    classes = value["class_ids"]
    if kind == "secondary_structure":
        raise PhenotypingPopulationError(
            "Secondary structures are detected after phenotyping. Choose all segmented cells, "
            "detected tissue or primary tissue classes for the phenotyping population."
        )
    if not isinstance(kind, str) or kind not in KINDS:
        raise PhenotypingPopulationError("The phenotyping region type is unsupported")
    if not isinstance(classes, (tuple, list)) or any(
        not isinstance(item, str) or not item or item != item.strip() for item in classes
    ) or len(set(classes)) != len(classes):
        raise PhenotypingPopulationError("Selected structure classes must be unique names")
    if (kind == "primary_structure") != bool(classes):
        raise PhenotypingPopulationError("Structure regions require at least one selected class")
    if "Ignore" in classes:
        raise PhenotypingPopulationError("Ignored regions cannot be selected for phenotyping")
    return {"kind": kind, "class_ids": sorted(classes)}


def validate_population_settings(value: object | None) -> dict[str, object]:
    """Normalize a shared selection and complete slide replacements.

    Missing settings preserve the historical all-segmented-cell population.
    Explicit detected-tissue selection requires an exported tissue membership;
    we never infer it from an analysis-region ID or a cell's display label.
    """
    if value is None:
        return {"default": {"kind": "all_segmented", "class_ids": []}, "slide_overrides": {}}
    if not isinstance(value, Mapping) or set(value) != {"default", "slide_overrides"}:
        raise PhenotypingPopulationError("Phenotyping population settings require a default and slide overrides")
    overrides = value["slide_overrides"]
    if not isinstance(overrides, Mapping):
        raise PhenotypingPopulationError("Slide population overrides must be keyed by complete slide identity")
    normalized = {}
    for key, selected in overrides.items():
        try:
            identity = json.loads(key)
            if not isinstance(identity, list) or len(identity) != 3 or slide_population_key(*identity) != key:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise PhenotypingPopulationError("A population override has an invalid slide identity") from exc
        normalized[key] = _selection(selected)
    return {"default": _selection(value["default"]), "slide_overrides": dict(sorted(normalized.items()))}


def resolve_population_selection(settings: object | None, *, sample_id: str,
                                 project_entry_id: str, slide_id: str) -> dict[str, object]:
    """Return the slide override, or the dataset default when no override exists."""
    checked = validate_population_settings(settings)
    return checked["slide_overrides"].get(
        slide_population_key(sample_id, project_entry_id, slide_id), checked["default"]
    )


def validate_offered_population(settings, options):
    """Admit a common decision only for the current images and available classes."""
    checked = validate_population_settings(settings)
    slides = {row["image_identity"]: row for row in options["slides"]}
    if set(checked["slide_overrides"]) - set(slides):
        raise PhenotypingPopulationError("A population override refers to another image")
    for key, row in slides.items():
        choice = checked["slide_overrides"].get(key, checked["default"])
        if choice["kind"] not in row["available_kinds"]:
            raise PhenotypingPopulationError("The selected regions are unavailable for " + row["slide_id"])
        field = {"primary_structure": "primary_classes"}.get(choice["kind"])
        if field and set(choice["class_ids"]) - {item["class_id"] for item in row[field]}:
            raise PhenotypingPopulationError("Choose current region classes for " + row["slide_id"])
    return checked


def require_population_sources(selection: Mapping[str, object], header: object) -> None:
    """Require completed source coverage before a selected cell can enter a fit.

    A source with an empty ROI inventory is complete and can select zero cells.
    An absent source is unavailable, never an empty successful classifier result.
    Every current analysis domain must have the requested accepted class source.
    The measurement reader checks each source's domain revision separately.
    """
    selected = _selection(selection)
    if selected["kind"] == "all_segmented":
        return
    sources = getattr(header, "population_sources", None)
    if sources is None:
        raise PhenotypingPopulationError("Refresh cell measurements to select tissue or structure regions for phenotyping")
    if selected["kind"] == "detected_tissue":
        return
    for domain in header.analysis_domain_ids:
        available = {class_id for source in sources
                     if source["kind"] == selected["kind"] and source["analysis_domain_id"] == domain
                     for class_id in source["class_ids"]}
        missing = set(selected["class_ids"]) - available
        if missing:
            raise PhenotypingPopulationError(
                "Accepted region results are missing for " + ", ".join(sorted(missing)) +
                "; finish the classifier and refresh cell measurements"
            )


def population_includes(selection: Mapping[str, object], membership: Mapping[str, object] | None) -> bool:
    """Select a cell once when its centroid belongs to any chosen structure class."""
    kind = selection["kind"]
    if kind == "all_segmented":
        return True
    if membership is None:
        raise PhenotypingPopulationError("Refresh cell measurements before restricting the phenotyping population")
    if kind == "detected_tissue":
        return membership["detected_tissue"] is True
    return bool(set(selection["class_ids"]).intersection(membership[kind]))
