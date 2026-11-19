"""Informational area-annotation counts from an already accepted native return.

These are saved user annotation objects, not sampled pixels, eligible training
observations or a declaration that reference requirements have passed. The native
import owns geometry validation and publication; this projection does no I/O.
"""

from collections import Counter
from collections.abc import Mapping


def saved_area_region_counts(objects, protected_ids):
    """Count classed area objects while preserving unknown for unrecognized input."""
    if not isinstance(objects, Mapping) or objects.get("type") != "FeatureCollection":
        return None
    features = objects.get("features")
    if not isinstance(features, list):
        return None
    protected = set(protected_ids)
    counts = Counter()
    unlabelled = 0
    for feature in features:
        if not isinstance(feature, Mapping) or not isinstance(feature.get("id"), str):
            return None
        if feature["id"] in protected:
            continue
        geometry = feature.get("geometry")
        properties = feature.get("properties")
        if not isinstance(geometry, Mapping) or not isinstance(properties, Mapping):
            return None
        if geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            continue
        classification = properties.get("classification")
        if classification is None:
            unlabelled += 1
            continue
        if not isinstance(classification, Mapping) or not isinstance(classification.get("name"), str):
            return None
        name = classification["name"]
        if name:
            counts[name] += 1
        else:
            unlabelled += 1
    return {"class_region_counts": dict(sorted(counts.items())), "unlabelled_regions": unlabelled}
