"""One accepted dye catalogue with independently checked cycle panels.

Each panel remains an ordinary full-rank reference library. Shared dyes retain
identical accepted spectra and original control evidence across those panels.
The union is a catalogue, not a claim that all dyes can be fit simultaneously.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json

from .reference_models import FluorophoreReferenceLibrary
from .reference_library import dumps_reference_artifact, loads_reference_artifact
from .typed_checksum import typed_checksum_sha256

SCHEMA = "mif_pipeline.shared_reference_catalogue/v1"
MAX_BYTES = 32 * 1024 * 1024


def _cycle_panels(values):
    from mif_pipeline.io.metadata.cycles import split_cycle_component
    from mif_pipeline.io.metadata.canonical import is_autofluorescence_component
    if type(values) is not tuple or len(values) < 2:
        raise ValueError("Shared references require at least two recorded cycle panels")
    for cycle, names in values:
        if (type(cycle) is not int or cycle < 0 or type(names) is not tuple or not names
                or len(set(names)) != len(names)
                or any(type(name) is not str or not name or name != name.strip()
                    or split_cycle_component(name)[1] is not None or is_autofluorescence_component(name) for name in names)):
            raise ValueError("Shared cycle panels require plain, unique fluorophore identities")
    if len({cycle for cycle, _ in values}) != len(values):
        raise ValueError("A shared reference cycle panel is repeated")
    return values


def shared_catalogue_metadata(ome, cycle_panels):
    """Review the unique dye inventory on a real common-band source view."""
    from mif_pipeline.io.metadata.canonical import (apply_component_identity_inventory,
        ComponentIdentityMode, COMPONENT_IDENTITY_NAMESPACE)
    panels = _cycle_panels(cycle_panels)
    components = tuple(dict.fromkeys(name for _, names in panels for name in names))
    projected = ome.model_copy(deep=True)
    removed = set() if projected.structured_annotations is None else {
        item.id for item in projected.structured_annotations.map_annotations
        if item.namespace == COMPONENT_IDENTITY_NAMESPACE}
    if projected.structured_annotations is not None:
        projected.structured_annotations.map_annotations = [item for item in
            projected.structured_annotations.map_annotations if item.id not in removed]
    projected.images[0].annotation_refs = [item for item in projected.images[0].annotation_refs
                                         if item.id not in removed]
    return apply_component_identity_inventory(projected, 0, components,
        mode=ComponentIdentityMode.PLANNED_INVENTORY, component_biomarkers=(None,) * len(components),
        nuclear_stain_reviewed=True, nuclear_component_index=None,
        decision_source="shared_reference_construction")

@dataclass(frozen=True, slots=True)
class SharedReferenceSession:
    source_checkpoint_sha256: str
    target_input_sha256: str
    target_image_sha256: str
    reviewer_id: str
    construction_run_id: str
    cycles: tuple[int, ...]
    cycle_run_ids: tuple[str, ...]
    imported_catalogue: SharedReferenceCatalogue | None = None
    content_sha256: str = field(init=False)

    def __post_init__(self):
        from dataclasses import fields
        for name in ("source_checkpoint_sha256", "target_input_sha256", "target_image_sha256"):
            value = getattr(self, name)
            if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("Shared reference session requires exact parent authority")
        if (type(self.cycles) is not tuple or len(self.cycles) < 2 or len(set(self.cycles)) != len(self.cycles)
                or any(type(cycle) is not int or cycle < 0 for cycle in self.cycles)
                or type(self.cycle_run_ids) is not tuple or len(self.cycle_run_ids) != len(self.cycles)
                or len(set(self.cycle_run_ids + (self.construction_run_id,))) != len(self.cycles) + 1):
            raise ValueError("Shared reference session cycle children differ")
        if any(type(value) is not str or not value or value != value.strip()
               for value in (self.reviewer_id, self.construction_run_id, *self.cycle_run_ids)):
            raise ValueError("Shared reference session identities must be literal text")
        if self.imported_catalogue is not None and (
                type(self.imported_catalogue) is not SharedReferenceCatalogue
                or self.imported_catalogue.cycles != self.cycles):
            raise ValueError("The imported shared catalogue differs from the session cycles")
        object.__setattr__(self, "content_sha256", typed_checksum_sha256({
            item.name: (list(getattr(self, item.name)) if type(getattr(self, item.name)) is tuple
                        else getattr(self, item.name).content_sha256 if item.name == "imported_catalogue" and self.imported_catalogue is not None
                        else getattr(self, item.name)) for item in fields(self) if item.init}))


@dataclass(frozen=True, slots=True)
class SharedConstructionScope:
    parent_target_sha256: str
    child_target_sha256: str
    cycle_panels: tuple[tuple[int, tuple[str, ...]], ...]
    content_sha256: str = field(init=False)

    def __post_init__(self):
        for value in (self.parent_target_sha256, self.child_target_sha256):
            if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("Shared reference construction scope requires exact target authorities")
        _cycle_panels(self.cycle_panels)
        object.__setattr__(self, "content_sha256", typed_checksum_sha256({
            "parent_target": self.parent_target_sha256, "child_target": self.child_target_sha256,
            "panels": [[cycle, list(names)] for cycle, names in self.cycle_panels]}))


@dataclass(frozen=True, slots=True)
class SharedCycleSourceDecision:
    session: SharedReferenceSession
    catalogue_sha256: str
    child_target_sha256s: tuple[str, ...]
    child_basis_sha256s: tuple[str, ...]
    content_sha256: str = field(init=False)

    def __post_init__(self):
        if type(self.session) is not SharedReferenceSession:
            raise TypeError("A shared cycle source requires its parent-bound session")
        for values in (self.child_target_sha256s, self.child_basis_sha256s):
            if type(values) is not tuple or len(values) != len(self.session.cycles):
                raise ValueError("Shared reference source must bind every cycle")
        for value in (self.catalogue_sha256, *self.child_target_sha256s, *self.child_basis_sha256s):
            if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("Shared reference source authority is invalid")
        object.__setattr__(self, "content_sha256", typed_checksum_sha256({
            "session": self.session.content_sha256, "catalogue": self.catalogue_sha256,
            "child_targets": list(self.child_target_sha256s), "child_bases": list(self.child_basis_sha256s)}))


@dataclass(frozen=True, slots=True)
class SharedReferenceCatalogue:
    artifact_id: str
    component_order: tuple[str, ...]
    cycles: tuple[int, ...]
    libraries: tuple[FluorophoreReferenceLibrary, ...]
    content_sha256: str = field(init=False)

    def __post_init__(self):
        if type(self.artifact_id) is not str or not self.artifact_id or self.artifact_id != self.artifact_id.strip():
            raise ValueError("A shared catalogue needs a literal artifact identity")
        if (type(self.component_order) is not tuple or not self.component_order
                or len(set(self.component_order)) != len(self.component_order)
                or any(type(name) is not str or not name or name != name.strip() for name in self.component_order)):
            raise ValueError("A shared catalogue needs unique fluorophore identities")
        if (type(self.cycles) is not tuple or len(self.cycles) < 2
                or any(type(cycle) is not int or cycle < 0 for cycle in self.cycles)
                or len(set(self.cycles)) != len(self.cycles)
                or type(self.libraries) is not tuple or len(self.libraries) != len(self.cycles)):
            raise ValueError("A shared catalogue needs one checked library for every distinct cycle")
        observed = {}
        for library in self.libraries:
            if type(library) is not FluorophoreReferenceLibrary:
                raise TypeError("Cycle panels must be ordinary accepted fluorophore libraries")
            # Reconstruct the immutable scientific artifact, including its rank,
            # normalization, ROI/source evidence and exact content identity.
            if loads_reference_artifact(dumps_reference_artifact(library)) != library:
                raise ValueError("A catalogue cycle library has changed")
            if library.axis != self.libraries[0].axis:
                raise ValueError("Shared reference panels must retain one exact acquired-band axis")
            for spectrum in library.spectra:
                name = spectrum.fluorophore_identity
                if name in observed and observed[name] != spectrum:
                    raise ValueError(f"Shared fluorophore {name} differs between cycle panels")
                observed[name] = spectrum
        if set(observed) != set(self.component_order):
            raise ValueError("The catalogue component order does not cover its cycle panels")
        object.__setattr__(self, "content_sha256", typed_checksum_sha256({
            "schema": SCHEMA, "artifact_id": self.artifact_id,
            "component_order": list(self.component_order), "cycles": list(self.cycles),
            "library_sha256s": [library.content_sha256 for library in self.libraries]}))

    @property
    def spectra(self):
        values = {spectrum.fluorophore_identity: spectrum
                  for library in self.libraries for spectrum in library.spectra}
        return tuple(values[name] for name in self.component_order)

    @property
    def axis(self): return self.libraries[0].axis

    @property
    def normalization_convention(self):
        conventions = {item.normalization_convention for item in self.libraries}
        if len(conventions) != 1:
            raise ValueError("A shared catalogue must retain one normalization convention")
        return next(iter(conventions))

    @property
    def cycle_panels(self):
        return tuple((cycle, tuple(item.fluorophore_identity for item in library.spectra))
                     for cycle, library in zip(self.cycles, self.libraries, strict=True))

    def for_cycle(self, cycle):
        if type(cycle) is not int or cycle not in self.cycles:
            raise ValueError("The shared catalogue has no accepted panel for this cycle")
        return self.libraries[self.cycles.index(cycle)]


def dumps_reference_catalogue(catalogue):
    if type(catalogue) is not SharedReferenceCatalogue:
        raise TypeError("Expected an exact shared reference catalogue")
    value = {"schema": SCHEMA, "artifact_id": catalogue.artifact_id,
             "component_order": list(catalogue.component_order), "cycles": list(catalogue.cycles),
             "libraries": [json.loads(dumps_reference_artifact(library)) for library in catalogue.libraries],
             "content_sha256": catalogue.content_sha256}
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Repeated shared reference catalogue field")
        result[key] = value
    return result


def loads_reference_catalogue(payload):
    if not isinstance(payload, (str, bytes)) or len(payload) > MAX_BYTES:
        raise ValueError("Invalid shared reference catalogue payload")
    value = json.loads(payload, object_pairs_hook=_unique_fields)
    if (type(value) is not dict or set(value) != {
            "schema", "artifact_id", "component_order", "cycles", "libraries", "content_sha256"}
            or value["schema"] != SCHEMA
            or any(type(value[name]) is not list for name in ("component_order", "cycles", "libraries"))):
        raise ValueError("Unsupported shared reference catalogue fields")
    record = SharedReferenceCatalogue(value["artifact_id"], tuple(value["component_order"]), tuple(value["cycles"]),
        tuple(loads_reference_artifact(json.dumps(library)) for library in value["libraries"]))
    if record.content_sha256 != value["content_sha256"]:
        raise ValueError("Shared reference catalogue checksum differs")
    return record


def dumps_reference_source(record):
    """Serialize either an ordinary simultaneous library or a cycle catalogue."""
    return (dumps_reference_catalogue(record) if type(record) is SharedReferenceCatalogue
            else dumps_reference_artifact(record))


def loads_reference_source(payload):
    """Dispatch the explicit catalogue schema without changing old artifact rules."""
    if type(payload) not in {str, bytes} or len(payload) > MAX_BYTES:
        raise ValueError("Invalid reviewed reference payload")
    value = json.loads(payload)
    return (loads_reference_catalogue(payload) if type(value) is dict and value.get("schema") == SCHEMA
            else loads_reference_artifact(payload))


def compose_shared_reference_catalogue(spectra, candidates, qc_policy, publication, cycle_panels, *, reviewer_id):
    """Compose ordinary per-cycle libraries from the once-reviewed dye spectra."""
    from dataclasses import replace
    from .reference_service_composition import compose_accepted_library
    if len(spectra) != len(candidates):
        raise ValueError("Shared reference spectra and consensus evidence differ")
    by_name = {spectrum.fluorophore_identity: (spectrum, candidate)
               for spectrum, candidate in zip(spectra, candidates, strict=True)}
    if len(by_name) != len(spectra):
        raise ValueError("A fluorophore must be extracted once in the shared catalogue")
    libraries = []
    for cycle, names in cycle_panels:
        if not names or any(name not in by_name for name in names):
            raise ValueError("A cycle panel references an unavailable accepted fluorophore")
        pairs = tuple(by_name[name] for name in names)
        libraries.append(compose_accepted_library(tuple(pair[0] for pair in pairs), tuple(pair[1] for pair in pairs),
            qc_policy, replace(publication, artifact_id=f"{publication.artifact_id}:cycle:{cycle}"),
            reviewer_id=reviewer_id))
    return SharedReferenceCatalogue(publication.artifact_id, tuple(by_name),
                                    tuple(cycle for cycle, _ in cycle_panels), tuple(libraries))
