"""Reuse one reviewed dye construction across actual cyclic channel views.

The parent retains its original target authority and final review. Internal
children extract the common dye catalogue once, then review AF on each actual
cycle. No child image is materialized or numerically unmixed here.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ome_types import from_xml

from .cycle_references import shared_cycle_reference_layout
from .reference_catalogue import (SharedReferenceCatalogue, SharedReferenceSession,
    SharedConstructionScope, SharedCycleSourceDecision, shared_catalogue_metadata)
from .typed_checksum import typed_checksum_sha256


def _optional_artifacts(service, **values):
    from .reference_service_artifacts import ReferenceArtifactRepositoryError
    try:
        return service._artifacts.load_all(**values)
    except ReferenceArtifactRepositoryError as error:
        if str(error) != "reference scientific artifact is missing":
            raise
        return ()


@dataclass(frozen=True, slots=True)
class SharedReferenceChild:
    run_id: str
    authority: object
    reviewed_ome: object
    phase: str
    cycle: int | None
    checkpoint_token: str
    parent_token: str


class SharedReferenceCoordinator:
    def __init__(self, service):
        self.service = service

    def _parent(self, run_id):
        return self.service._basis_choice_inputs(run_id)[:3]

    def _session(self, run_id, start):
        records = _optional_artifacts(self.service, run_id=run_id, start_authority_sha256=start,
            kind="shared_reference_session", subject_id="shared-references")
        if len(records) > 1:
            raise ValueError("Shared reference session authority is ambiguous")
        return None if not records else records[0].record

    def has_session(self, parent_run_id):
        _current, start = self.service._store._load_service_checkpoint(parent_run_id)
        return self._session(parent_run_id, start) is not None

    def persisted_child(self, parent_run_id):
        """Read retained child state without creating views, sampling or advancing."""
        from .reference_workflow import ReferenceWorkflowStatus
        from .reference_service_storage import ReferenceCheckpointStoreError
        from .reference_service_records import ReferenceServiceSnapshot
        current, start = self.service._store._load_service_checkpoint(parent_run_id)
        session = self._session(parent_run_id, start)
        if session is None or current.basis_source == "shared_cycle_reference_library":
            return None
        target = self.service._require_canonical_target(parent_run_id, start)
        if (current.content_sha256 != session.source_checkpoint_sha256
                or target.authority_sha256 != session.target_input_sha256):
            raise ValueError("The shared reference parent authority has changed")
        if session.imported_catalogue is None:
            try:
                construction = ReferenceServiceSnapshot(*self.service._store._load_service_checkpoint(session.construction_run_id))
            except ReferenceCheckpointStoreError as error:
                if str(error) != "checkpoint run does not exist": raise
                return None
            if construction.checkpoint.accepted_library is None:
                return self._child(session, construction, cycle=None)
        for cycle, run_id in zip(session.cycles, session.cycle_run_ids, strict=True):
            try:
                snapshot = ReferenceServiceSnapshot(*self.service._store._load_service_checkpoint(run_id))
            except ReferenceCheckpointStoreError as error:
                if str(error) != "checkpoint run does not exist": raise
                return None
            if snapshot.checkpoint.status is not ReferenceWorkflowStatus.COMPLETED:
                return self._child(session, snapshot, cycle=cycle)
        return None

    def start(self, parent_run_id, expected_checkpoint_token, *, reviewer_id, imported_library=None):
        current, start, target = self._parent(parent_run_id)
        if (current.content_sha256 != expected_checkpoint_token or current.request is None
                or current.request.checkpoint.value != "basis_source"):
            raise ValueError("The shared reference source choice has changed")
        layout = shared_cycle_reference_layout(target, current.af_behavior)
        if layout is None:
            raise ValueError("Shared reference construction requires recorded multiple cycles")
        prefix = "shared-reference-" + typed_checksum_sha256({
            "parent": parent_run_id, "checkpoint": current.content_sha256})[:24]
        session = SharedReferenceSession(current.content_sha256, target.authority_sha256,
            target.evidence.source_image_sha256, reviewer_id, prefix + "-construction", layout.cycles,
            tuple(prefix + f"-cycle-{cycle}" for cycle in layout.cycles),
            None if imported_library is None else compatible_imported_catalogue(self.service, current, target, imported_library))
        self.service._artifacts.put_unique(run_id=parent_run_id, start_authority_sha256=start,
            authority_checkpoint_sha256=current.content_sha256, kind="shared_reference_session",
            subject_id="shared-references", record=session)
        return self.active(parent_run_id)

    def _ensure_child(self, session, target, layout, *, cycle=None):
        from mif_pipeline.io import InspectedSourceAuthority
        from mif_pipeline.io.source_channel_view import write_source_channel_view
        from mif_pipeline.io.metadata.canonical import is_autofluorescence_component
        from mif_pipeline.io.metadata.cycles import split_cycle_component
        from mif_pipeline.preprocessing.inspection.workflow import inspect_opened_wsi
        from .reference_service_storage import ReferenceCheckpointStoreError

        construction = cycle is None
        panels = tuple((value, tuple(split_cycle_component(name)[0] for name in target.component_names
            if split_cycle_component(name)[1] == value and not is_autofluorescence_component(name)))
            for value in layout.cycles)
        position = 0 if construction else session.cycles.index(cycle)
        run_id = session.construction_run_id if construction else session.cycle_run_ids[position]
        binding = target.locator_binding
        authority = InspectedSourceAuthority.from_json_bytes(binding.inspected_authority_json.encode())
        reviewed = from_xml(binding.reviewed_ome_xml)
        root = Path(self.service._store.output_root) / "shared-reference-views" / session.content_sha256
        expected_path = root / f"cycle-{session.cycles[position]}.mif-channel-view.json"
        try:
            snapshot = self.service.resume(run_id)
        except ReferenceCheckpointStoreError as exc:
            # Only absent runs may be created. Corrupt saved state must fail closed.
            if str(exc) != "checkpoint run does not exist":
                raise
            path = write_source_channel_view(expected_path,
                authority, reviewed, layout.band_indices[position])
            with self.service._sampling_backend().open(path) as source:
                workflow, child_authority = inspect_opened_wsi(path, source)
            child_reviewed = workflow.metadata
            if construction:
                child_reviewed = shared_catalogue_metadata(child_reviewed, panels)
            snapshot = self.service.start_from_reviewed_target(run_id, child_authority, child_reviewed,
                af_behavior="already_removed" if construction else self._af_behavior)
        child_target = self.service._require_canonical_target(run_id, snapshot.start_authority_sha256)
        from mif_pipeline.io.source_channel_view import selected_channel_metadata
        expected_ome = selected_channel_metadata(reviewed, layout.band_indices[position])
        if construction:
            expected_ome = shared_catalogue_metadata(expected_ome, panels)
        if (Path(child_target.locator_binding.source_locator) != expected_path
                or from_xml(child_target.locator_binding.reviewed_ome_xml).model_dump(mode="json") != expected_ome.model_dump(mode="json")):
            raise ValueError("The reference child differs from its original parent channels or component panel")
        write_source_channel_view(expected_path, authority, reviewed, layout.band_indices[position])
        if construction:
            scope = SharedConstructionScope(target.authority_sha256, child_target.authority_sha256, panels)
            existing = _optional_artifacts(self.service, run_id=run_id,
                start_authority_sha256=snapshot.start_authority_sha256,
                kind="shared_construction_scope", subject_id="shared-construction")
            if existing:
                if len(existing) != 1 or existing[0].record != scope:
                    raise ValueError("Shared reference construction scope has changed")
            else:
                self.service._artifacts.put_unique(run_id=run_id, start_authority_sha256=snapshot.start_authority_sha256,
                    authority_checkpoint_sha256=snapshot.checkpoint.content_sha256,
                    kind="shared_construction_scope", subject_id="shared-construction", record=scope)
        return snapshot

    def _catalogue(self, session):
        if session.imported_catalogue is not None:
            return session.imported_catalogue
        snapshot = self.service.resume(session.construction_run_id)
        pointer = snapshot.checkpoint.accepted_library
        if pointer is None:
            return None
        catalogue = self.service._artifacts.load_pointer(run_id=session.construction_run_id,
            start_authority_sha256=snapshot.start_authority_sha256, kind="accepted_library",
            subject_id="library", pointer=pointer).record
        if type(catalogue) is not SharedReferenceCatalogue or catalogue.cycles != session.cycles:
            raise ValueError("The shared construction has no matching accepted cycle catalogue")
        return catalogue

    def _child(self, session, snapshot, *, cycle):
        from mif_pipeline.io import InspectedSourceAuthority
        target = self.service._require_canonical_target(snapshot.checkpoint.run_id, snapshot.start_authority_sha256)
        token = typed_checksum_sha256({"shared_session": session.content_sha256,
            "child_run": snapshot.checkpoint.run_id, "child_checkpoint": snapshot.checkpoint.content_sha256})
        return SharedReferenceChild(snapshot.checkpoint.run_id,
            InspectedSourceAuthority.from_json_bytes(target.locator_binding.inspected_authority_json.encode()),
            from_xml(target.locator_binding.reviewed_ome_xml), "construction" if cycle is None else "cycle",
            cycle, snapshot.checkpoint.content_sha256, token)

    def active(self, parent_run_id, *, advance=True):
        if not advance:
            return self.persisted_child(parent_run_id)
        from .reference_workflow import ReferenceWorkflowStatus
        current, start, target = self._parent(parent_run_id)
        session = self._session(parent_run_id, start)
        if session is None:
            return None
        if (session.target_input_sha256 != target.authority_sha256
                or session.target_image_sha256 != target.evidence.source_image_sha256):
            raise ValueError("The shared reference parent target has changed")
        if current.basis_source == "shared_cycle_reference_library":
            return None
        if current.content_sha256 != session.source_checkpoint_sha256:
            raise ValueError("The shared reference parent checkpoint has changed")
        layout = shared_cycle_reference_layout(target, current.af_behavior)
        self._af_behavior = current.af_behavior
        if session.imported_catalogue is None:
            snapshot = self._ensure_child(session, target, layout)
            if snapshot.checkpoint.basis_source is None:
                snapshot = self.service.accept_basis_source_choice(snapshot.checkpoint.run_id,
                    snapshot.checkpoint.content_sha256, "construct_reference_library",
                    reviewer_id=session.reviewer_id).snapshot
        catalogue = self._catalogue(session)
        if catalogue is None:
            return self._child(session, snapshot, cycle=None)
        for cycle in session.cycles:
            snapshot = self._ensure_child(session, target, layout, cycle=cycle)
            if snapshot.checkpoint.basis_source is None and advance:
                snapshot = self.service.accept_basis_source_choice(snapshot.checkpoint.run_id,
                    snapshot.checkpoint.content_sha256, "import_reference_library",
                    reviewer_id=session.reviewer_id, imported_library=catalogue.for_cycle(cycle)).snapshot
            if snapshot.checkpoint.status is not ReferenceWorkflowStatus.COMPLETED:
                return self._child(session, snapshot, cycle=cycle)
        if advance:
            self._publish_parent_source(parent_run_id, current, start, target, session, catalogue)
        return None

    def translate_token(self, parent_run_id, parent_token):
        child = self.active(parent_run_id, advance=False)
        if child is None or child.parent_token != parent_token:
            raise ValueError("The shared reference review is stale or reordered")
        return child.checkpoint_token

    def translate_stopped_tokens(self, parent_run_id, current_parent_token, previous_parent_token):
        """Authenticate wrapped Stop/recovery tokens against one saved child history."""
        child = self.persisted_child(parent_run_id)
        if child is None:
            raise ValueError("No shared reference child is available to reopen")
        _parent, start = self.service._store._load_service_checkpoint(parent_run_id)
        session = self._session(parent_run_id, start)
        current, _child_start = self.service._store._load_service_checkpoint(child.run_id)
        def wrapped(checkpoint):
            return typed_checksum_sha256({"shared_session": session.content_sha256,
                "child_run": child.run_id, "child_checkpoint": checkpoint})
        stopped = current.content_sha256
        previous = current.prior_checkpoint_sha256
        if wrapped(stopped) != current_parent_token:
            # A successful reopen may have preceded publication of its parent form.
            stopped = current.prior_checkpoint_sha256
            previous = None if not current.decisions else current.decisions[-1].source_checkpoint_sha256
        if (stopped is None or previous is None or wrapped(stopped) != current_parent_token
                or wrapped(previous) != previous_parent_token):
            raise ValueError("The stopped shared reference review has changed")
        self.service.reopen_stopped_review(child.run_id, stopped, previous, publish=False)
        return child.run_id, stopped, previous

    def _publish_parent_source(self, run_id, current, start, target, session, catalogue):
        from .interactions import configured_response
        from .reference_workflow import advance_reference_workflow
        targets, artifacts = _accepted_children(self.service, session)
        record = SharedCycleSourceDecision(session, catalogue.content_sha256,
            tuple(item.authority_sha256 for item in targets), tuple(item.content_sha256 for item in artifacts))
        self.service._artifacts.put_unique(run_id=run_id, start_authority_sha256=start,
            authority_checkpoint_sha256=current.content_sha256, kind="shared_cycle_source",
            subject_id="shared-cycle-source", record=record)
        response = configured_response(current.request, option_value="shared_cycle_reference_library",
                                       reviewer_id=session.reviewer_id)
        candidate = advance_reference_workflow(current, response)
        self.service._store._compare_and_swap_service(current.content_sha256, candidate, start)


def compatible_imported_catalogue(service, current, target, source):
    """Match already checked panels to the actual target cycles and calibration."""
    from dataclasses import replace
    from types import SimpleNamespace
    from .reference_models import FluorophoreReferenceLibrary, SpectralMeasurementAxis
    from .reference_catalogue import dumps_reference_source, loads_reference_source
    from mif_pipeline.io.metadata.cycles import split_cycle_component
    from mif_pipeline.io.metadata.canonical import is_autofluorescence_component
    if type(source) not in {SharedReferenceCatalogue, FluorophoreReferenceLibrary}:
        raise ValueError("Select a complete accepted fluorophore library or shared catalogue")
    source = loads_reference_source(dumps_reference_source(source))
    layout = shared_cycle_reference_layout(target, current.af_behavior)
    if layout is None:
        raise ValueError("A shared catalogue needs a target with recorded imaging cycles")
    available = source.libraries if type(source) is SharedReferenceCatalogue else (source,)
    libraries, components = [], []
    for cycle, indices in zip(layout.cycles, layout.band_indices, strict=True):
        names = tuple(split_cycle_component(name)[0] for name in target.component_names
            if split_cycle_component(name)[1] == cycle and not is_autofluorescence_component(name))
        matches = tuple(item for item in available
            if {spectrum.fluorophore_identity for spectrum in item.spectra} == set(names))
        if not matches:
            raise ValueError(f"The reviewed library has no checked fluorophore panel for cycle {cycle + 1}")
        library = matches[0]
        axis = SpectralMeasurementAxis(tuple(replace(target.evidence.axis.measurements[index], parent_band_id=band)
            for index, band in zip(indices, layout.band_names, strict=True)))
        evidence = replace(target.evidence, axis=axis,
            quantitative_signature=replace(target.evidence.quantitative_signature,
                measurements=tuple(target.evidence.quantitative_signature.measurements[index] for index in indices)))
        service._source_choice_operations._validate_imported_library(
            SimpleNamespace(fluorophores=names), SimpleNamespace(evidence=evidence), library)
        libraries.append(library)
        components.extend(name for name in names if name not in components)
    return SharedReferenceCatalogue("reused:" + source.content_sha256, tuple(components), layout.cycles, tuple(libraries))


def _accepted_children(service, session):
    targets, artifacts = [], []
    for run_id in session.cycle_run_ids:
        application = service.load_final_basis_application(run_id)
        snapshot = service.resume(run_id)
        targets.append(service._require_canonical_target(run_id, snapshot.start_authority_sha256))
        # This has just been revalidated by the service's final-authority loader.
        artifacts.append(application._authority[0])
    return tuple(targets), tuple(artifacts)


def compose_shared_cycle_basis(service, current, start, target, source_checkpoint_sha256, qc_policy):
    from .final_basis import FinalBasisSourceBinding, FinalBasisCompatibilityEvidence, FinalRunBasisProposal
    from .reference_models import BasisSourceLineage, ResolvedBasisColumn
    from .target_af import evaluate_run_basis_qc
    from mif_pipeline.io.metadata.cycles import split_cycle_component
    from mif_pipeline.io.metadata.canonical import is_autofluorescence_component
    records = service._artifacts.load_all(run_id=current.run_id, start_authority_sha256=start,
        kind="shared_cycle_source", subject_id="shared-cycle-source")
    if len(records) != 1:
        raise ValueError("The accepted shared cycle source is missing or ambiguous")
    record = records[0].record
    session = record.session
    if session.target_input_sha256 != target.authority_sha256:
        raise ValueError("The shared reference basis belongs to a different parent target")
    catalogue = SharedReferenceCoordinator(service)._catalogue(session)
    targets, artifacts = _accepted_children(service, session)
    if (catalogue.content_sha256 != record.catalogue_sha256
            or tuple(item.authority_sha256 for item in targets) != record.child_target_sha256s
            or tuple(item.content_sha256 for item in artifacts) != record.child_basis_sha256s):
        raise ValueError("Accepted cycle reference authority has changed")
    layout = shared_cycle_reference_layout(target, current.af_behavior)
    columns, bindings = [], []
    for cycle, indices, child, artifact in zip(layout.cycles, layout.band_indices, targets, artifacts, strict=True):
        if child.evidence.axis.parent_band_ids != layout.band_names:
            raise ValueError("An accepted cycle basis differs from its actual acquired bands")
        binding = FinalBasisSourceBinding(f"accepted_cycle_basis:{cycle}", artifact.artifact_id,
                                         artifact.content_sha256, artifact.proposal.run_id)
        bindings.append(binding)
        lineage = BasisSourceLineage("accepted_cycle_basis", binding.artifact_id,
                                    binding.artifact_sha256, binding.run_id)
        for name in layout.output_component_names:
            base, name_cycle = split_cycle_component(name)
            if name_cycle != cycle:
                continue
            matches = tuple(item for item in artifact.proposal.columns
                if (item.component_kind != "fluorophore" if is_autofluorescence_component(name)
                    else item.fluorophore_identity == base))
            if len(matches) != 1:
                raise ValueError("The accepted cycle basis does not cover the reviewed component panel")
            item = matches[0]
            values = [0.] * len(target.evidence.axis.measurements)
            for index, value in zip(indices, item.values, strict=True): values[index] = value
            columns.append(ResolvedBasisColumn(name, item.fluorophore_identity, item.component_kind,
                tuple(values), item.normalization_convention, item.abundance_rescaling_factor, lineage))
    by_name = {item.component_identity: item for item in columns}
    columns = tuple(by_name[name] for name in layout.output_component_names)
    bindings = tuple(bindings)
    return FinalRunBasisProposal(source_kind="cycle_references", source_choice="shared_cycle_reference_library",
        af_behavior=current.af_behavior, source_checkpoint_sha256=source_checkpoint_sha256,
        channel_fluors=current.fluorophores, run_id=current.run_id,
        target_image_sha256=target.evidence.source_image_sha256, axis=target.evidence.axis,
        columns=columns, qc_policy=qc_policy, qc=evaluate_run_basis_qc(columns, target.evidence.axis, qc_policy),
        compatibility=FinalBasisCompatibilityEvidence("cycle_references", (), (), None,
            tuple(item.artifact_sha256 for item in bindings)), source_bindings=bindings,
        quantitative_signature=target.evidence.quantitative_signature, ssasu_context=None)
