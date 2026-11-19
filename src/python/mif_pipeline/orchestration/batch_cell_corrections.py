"""An explicitly reviewed set of known-cell edits in one accepted segmentation.

The immutable segmentation and optional preceding correction own the complete
image, section and population scope. The existing native draft ticket binds the
complete operation list and returned compartment outlines.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import re
import hashlib

from .batch_artifacts import (ArtifactEquivalenceContract, ArtifactIdentityContract, DispatchMode, StageAdapterSpec,
    BatchArtifactError, BatchCommittedArtifactAuthority, validate_committed_artifact_authority_set)


KIND = "segmentation_correction"
OUTPUT = "segmentation_correction_result"


@dataclass(frozen=True, slots=True)
class CellCorrectionOperation:
    cell_id: str
    operation: str
    source_cell_ids: tuple[str, ...] = ()

    def __post_init__(self):
        if type(self.cell_id) is not str or not self.cell_id or self.cell_id.strip() != self.cell_id:
            raise ValueError("Cell correction requires its exact cell ID")
        if self.operation not in {"cell_outline", "nucleus_outline", "delete", "split", "merge", "add_cell", "add_nucleus"}:
            raise ValueError("Choose an outline correction")
        sources = self.source_cell_ids
        if not isinstance(sources, (tuple, list)) or any(
                type(value) is not str or not value or value.strip() != value for value in sources):
            raise ValueError("Merge requires the selected cell IDs")
        sources = tuple(sources)
        if self.operation == "merge":
            if len(sources) < 2 or sources != tuple(sorted(set(sources))) or self.cell_id != sources[0]:
                raise ValueError("Merge requires at least two distinct cells in their saved order")
        elif sources:
            raise ValueError("Only a merge selects multiple source cells")
        object.__setattr__(self, "source_cell_ids", sources)

    def to_mapping(self):
        value = dict(cell_id=self.cell_id, operation=self.operation)
        if self.source_cell_ids:
            value["source_cell_ids"] = list(self.source_cell_ids)
        return value


def correction_operations(values):
    """A source cell belongs to one operation, or a pair of compartment edits."""
    if not isinstance(values, (tuple, list)) or not values:
        raise ValueError("Select at least one cell correction")
    result = []
    for value in values:
        if isinstance(value, Mapping) and set(value) in ({"cell_id", "operation"}, {"cell_id", "operation", "source_cell_ids"}):
            value = CellCorrectionOperation(**value)
        if type(value) is not CellCorrectionOperation:
            raise ValueError("Each correction must identify its cells and operation")
        result.append(value)
    if len(set(result)) != len(result):
        raise ValueError("The same cell operation is selected more than once")
    by_source = {}
    for item in result:
        for cell_id in item.source_cell_ids or (item.cell_id,):
            by_source.setdefault(cell_id, []).append(item.operation)
    for selected in by_source.values():
        if len(selected) > 1 and set(selected) not in (
                {"cell_outline", "nucleus_outline"}, {"cell_outline", "add_nucleus"}):
            raise ValueError("A cell cannot belong to more than one correction; combine only cell and nuclear outline edits")
    return tuple(sorted(result, key=lambda item: (item.cell_id, item.operation)))


_REQUEST_BINDINGS = ("predecessor_node_id", "segmentation_result_ref_sha256",
    "previous_correction_ref_sha256", "ticket_id", "submission_sha256")


@dataclass(frozen=True, slots=True)
class CellCorrectionRequest:
    predecessor_node_id: str
    segmentation_result_ref_sha256: str
    previous_correction_ref_sha256: str | None
    operations: tuple[CellCorrectionOperation, ...]
    ticket_id: str
    submission_sha256: str
    _v1: bool = field(default=False, repr=False, compare=False, kw_only=True)

    def __post_init__(self):
        patterns = {
            "predecessor_node_id": r"node-[0-9a-f]{48}",
            "segmentation_result_ref_sha256": r"[0-9a-f]{64}",
            "previous_correction_ref_sha256": r"[0-9a-f]{64}",
            "ticket_id": r"[0-9a-f]{32}",
            "submission_sha256": r"[0-9a-f]{64}",
        }
        for name, pattern in patterns.items():
            value = getattr(self, name)
            if name == "previous_correction_ref_sha256" and value is None:
                continue
            if type(value) is not str or re.fullmatch(pattern, value) is None:
                raise ValueError(f"Cell correction has an invalid {name}")
        object.__setattr__(self, "operations", correction_operations(self.operations))
        if self._v1 and len(self.operations) != 1:
            raise ValueError("A saved single-cell request must contain one operation")

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping):
            raise ValueError("Cell correction requires its exact reviewed draft binding")
        if set(value) == {*_REQUEST_BINDINGS, "cell_id", "operation"}:
            # Preserve the exact serialized material of genuine accepted v1
            # requests. Current preparation always writes the operation list.
            return cls(**{name: value[name] for name in _REQUEST_BINDINGS},
                operations=(CellCorrectionOperation(value["cell_id"], value["operation"]),), _v1=True)
        if set(value) != {*_REQUEST_BINDINGS, "operations"}:
            raise ValueError("Cell correction requires its exact reviewed draft binding")
        return cls(**value)

    def to_mapping(self):
        body = {name: getattr(self, name) for name in _REQUEST_BINDINGS}
        if self._v1:
            body.update(self.operations[0].to_mapping())
        else:
            body["operations"] = [item.to_mapping() for item in self.operations]
        return body

    @property
    def cell_id(self):
        if len(self.operations) != 1:
            raise ValueError("This correction contains multiple operations")
        return self.operations[0].cell_id

    @property
    def operation(self):
        if len(self.operations) != 1:
            raise ValueError("This correction contains multiple operations")
        return self.operations[0].operation


def _correction_request_identity(selected):
    identity = selected.to_mapping()
    if not selected._v1:
        # Identity paths project through every operation in a mixed batch.
        for operation in identity["operations"]:
            operation.setdefault("source_cell_ids", [])
    return identity


def correction_result_identity_contract(*, v1=False):
    operation_fields = ("cell_id", "operation") if v1 else ("operations.cell_id", "operations.operation")
    return ArtifactIdentityContract(
        contract_id="mif-segmentation-correction-identity/" + ("v1" if v1 else "v2"), artifact_kind=OUTPUT,
        schema_name="roi-generation-manifest", schema_version=1,
        required_fields=tuple(sorted(("node_id", "attempt_id", "section_id",
            "segmentation_population_id", "segmentation_result_ref_sha256",
            "previous_correction_ref_sha256", "cell_edits.path", "cell_edits.sha256",
            *("correction_request." + name for name in (*_REQUEST_BINDINGS, *operation_fields)),
            "geometry_revision", "cell_count", "execution_exchange_sha256", "validated_exchange_sha256"))),
        optional_fields=() if v1 else ("correction_request.operations.source_cell_ids",),
        required_runtime_names=("mif_pipeline", "qupath"))


def build_cell_correction_spec():
    from .batch_artifacts import ArtifactMultiplicity
    from .batch_dag import ResourceKind
    from .batch_stage_adapters import _requirement

    return StageAdapterSpec(
        node_kind=KIND, node_version="2",
        required_inputs=(_requirement("segmentation_result", ArtifactMultiplicity.SINGLE),),
        optional_inputs=(_requirement(OUTPUT, ArtifactMultiplicity.SINGLE),),
        declared_outputs=(OUTPUT,), output_identity_contracts=(correction_result_identity_contract(),),
        compatibility_signature_id="mif-segmentation-correction-compatibility/v2",
        equivalence_contract=ArtifactEquivalenceContract(
            contract_id="mif-segmentation-correction-equivalence/v1", byte_deterministic=False,
            canonical_identity_paths=((OUTPUT, "segmentation_result_ref_sha256"),),
            stable_inventory_paths=((OUTPUT, "cell_count"),),
            scientific_value_paths=((OUTPUT, "cell_edits.sha256"),),
            qc_evidence_paths=((OUTPUT, "validated_exchange_sha256"),)),
        resource_kind=ResourceKind.QUPATH_IMAGE_WRITE.value,
        dispatch_mode=DispatchMode.QUPATH_PROJECT_CURRENT_IMAGE)


def append_cell_corrections(builder, segmentation, accepted_segmentation, section_scope):
    """Add only the explicit chain selected over this accepted segmentation."""
    pending = [item for item in builder.cell_corrections
               if item.segmentation_result_ref_sha256 == accepted_segmentation.artifact_ref_sha256]
    current = accepted = None
    while pending:
        preceding = None if accepted is None else accepted.artifact_ref_sha256
        candidates = [item for item in pending if item.previous_correction_ref_sha256 == preceding]
        if len(candidates) != 1:
            raise ValueError("Cell correction chain lacks one exact accepted predecessor")
        selected = candidates[0]
        inputs = ((segmentation, "segmentation_result", accepted_segmentation),)
        if current is not None:
            inputs += ((current, OUTPUT, accepted),)
        current = builder.add(KIND, **section_scope, inputs=inputs, cell_correction=selected)
        accepted = builder.accepted_by_producer_kind.get((current.node.node_id, OUTPUT))
        pending.remove(selected)
        if accepted is None and pending:
            raise ValueError("Finish the current correction before accepting another cell edit")
    return current, accepted


def decode_committed_cell_correction(authority, segmentation_ref):
    """Read the one accepted sparse selection bound to this immutable base."""
    if type(authority) is not BatchCommittedArtifactAuthority:
        raise BatchArtifactError("Cell corrections require committed state authority")
    validate_committed_artifact_authority_set((authority,))
    ref = authority.artifact_ref
    identity = ref.owning_identity
    selected = CellCorrectionRequest.from_mapping(identity["correction_request"])
    if (authority.producer_node_kind != KIND or ref.identity_contract != correction_result_identity_contract(v1=selected._v1)
            or ref.to_mapping()["owning_identity"]["correction_request"] != _correction_request_identity(selected)
            or ref.scope != segmentation_ref.scope or ref.configuration_bindings != segmentation_ref.configuration_bindings
            or identity["segmentation_result_ref_sha256"] != segmentation_ref.artifact_ref_sha256
            or identity["section_id"] != segmentation_ref.owning_identity["section_id"]
            or identity["segmentation_population_id"] != segmentation_ref.owning_identity["segmentation_population_id"]
            or identity["node_id"] != ref.producer_node_id or identity["attempt_id"] != ref.producer_attempt_id
            or identity["execution_exchange_sha256"] != authority.execution_exchange_sha256
            or type(identity["geometry_revision"]) is not int or identity["geometry_revision"] < 1
            or type(identity["cell_count"]) is not int or identity["cell_count"] < 0):
        raise BatchArtifactError("Cell correction differs from its accepted segmentation")
    if (selected.segmentation_result_ref_sha256 != segmentation_ref.artifact_ref_sha256
            or selected.previous_correction_ref_sha256 != identity["previous_correction_ref_sha256"]):
        raise BatchArtifactError("Cell correction differs from its reviewed draft")
    return identity


def current_cell_correction(snapshot, segmentation_ref):
    """Select the end of the current accepted chain, including during a new draft."""
    current = {item.artifact_ref.artifact_ref_sha256: item
               for item in snapshot.committed_artifact_authorities
               if item.artifact_ref.artifact_kind == OUTPUT
               and item.artifact_ref.owning_identity["segmentation_result_ref_sha256"] == segmentation_ref.artifact_ref_sha256}
    if not current:
        return None
    parents = {decode_committed_cell_correction(item, segmentation_ref)["previous_correction_ref_sha256"]
               for item in current.values()}
    latest = set(current) - parents
    if len(latest) != 1 or parents - {None} - set(current):
        raise BatchArtifactError("Current cell corrections lack one accepted chain")
    return current[latest.pop()]


def measurement_geometry_selection(measurement_ref, segmentation_ref, correction_ref_sha256):
    """Read the completed measurement's exact cumulative geometry selection.

    Its sparse checkpoint can include owned failure-recovery deletions after
    the preceding correction. The existing measurement owner already validated
    its rows; this read binds that immutable selection to the same base/chain.
    """
    from .batch_measurement_artifacts import validate_measurements_result_artifact_ref
    from mif_pipeline.cell_analysis.measurement_rows import (
        MarkerIntensityRowsDescriptor, read_measurement_dataset_selection,
    )
    measurement = validate_measurements_result_artifact_ref(measurement_ref)
    identity = measurement_ref.owning_identity
    if (measurement_ref.scope != segmentation_ref.scope
            or identity['segmentation_result_ref_sha256'] != segmentation_ref.artifact_ref_sha256):
        raise BatchArtifactError('The measured geometry differs from the accepted segmentation.')
    if identity.get('segmentation_correction_ref_sha256') != correction_ref_sha256:
        raise BatchArtifactError('The measured geometry differs from the current correction chain.')
    rows = measurement.measurement_rows
    selection = read_measurement_dataset_selection(
        MarkerIntensityRowsDescriptor(rows.path, rows.sha256, rows.byte_count, rows.row_count),
        authenticated_output_root=rows.path.parent.parent.parent)
    if selection['cell_dataset']['sha256'] != segmentation_ref.owning_identity['cell_evidence_sha256']:
        raise BatchArtifactError('The measured geometry differs from the accepted cell base.')
    if selection['cell_edits'] is None and selection['geometry_revision'] != 0:
        raise BatchArtifactError('The measured geometry lacks its sparse checkpoint.')
    return {**selection, 'cell_count':measurement.cell_count}


def current_measurement_geometry(snapshot, segmentation_ref, correction):
    """Return only the currently accepted measurements over this exact base."""
    matches = [item for item in snapshot.committed_artifact_authorities
        if item.artifact_ref.artifact_kind == 'measurements_result'
        and item.artifact_ref.owning_identity['segmentation_result_ref_sha256'] == segmentation_ref.artifact_ref_sha256]
    if len(matches) > 1:
        raise BatchArtifactError('The segmentation has more than one current measurement selection.')
    if not matches:
        return None, None
    authority = matches[0]
    return authority, measurement_geometry_selection(authority.artifact_ref, segmentation_ref,
        None if correction is None else correction.artifact_ref.artifact_ref_sha256)


def correction_source_selection(state_store, selected, submission, segmentation_ref, previous):
    """Reopen the bound completed predecessor, retained as revision evidence.

    The transition validates its current availability before retiring it. Native
    invocation reopens the same immutable terminal after that retirement, as
    the existing measurement-settings revision does for its predecessor.
    """
    source = submission['scope'].get('measurement_source')
    if source is None:
        return previous
    if not isinstance(source, Mapping) or set(source) != {'terminal_record_sha256', 'artifact_ref_sha256'}:
        raise BatchArtifactError('The correction lacks its exact completed measurement source.')
    terminal = state_store.load_attempt(source['terminal_record_sha256'])
    from mif_pipeline.pipeline_core import StageStatus
    if (terminal.node_id != selected.predecessor_node_id
            or terminal.stage_result.stage != 'measurements'
            or terminal.stage_result.status is not StageStatus.COMPLETED
            or len(terminal.output_artifacts) != 1
            or terminal.output_artifacts[0].artifact_ref_sha256 != source['artifact_ref_sha256']):
        raise BatchArtifactError('The correction changed its completed measurement predecessor.')
    return measurement_geometry_selection(terminal.output_artifacts[0], segmentation_ref,
                                         selected.previous_correction_ref_sha256)


def measurement_revision_for_correction(state_store, selected, submission, segmentation_ref):
    """Keep completed measurement choices when this correction becomes accepted.

    Measurements are deferred while the native correction is pending. Rebuild
    their existing typed settings revision from the bound terminal once that
    correction is accepted, so the successor task keeps the exact features.
    """
    source = submission['scope'].get('measurement_source')
    if source is None:
        return None
    correction_source_selection(state_store, selected, submission, segmentation_ref, None)
    from .batch_measurement_artifacts import validate_measurements_result_artifact_ref
    from .batch_measurement_settings import MeasurementSettings, MeasurementSettingsRevision
    terminal = state_store.load_attempt(source['terminal_record_sha256'])
    measurement = validate_measurements_result_artifact_ref(terminal.output_artifacts[0])
    original = state_store._load_plan_snapshot(submission['plan_sha256']).node_by_id[selected.predecessor_node_id]
    prior = original.get('measurement_settings_revision')
    features = ('feature_id', 'measurement_name', 'kind', 'channel_id', 'biomarker', 'compartment', 'units', 'opt_in')
    settings = MeasurementSettings.from_mapping(dict(canonical_channels=measurement.canonical_channels,
        feature_requests=[{key:item[key] for key in features} for item in measurement.measurement_inventory]))
    previous = None if prior is None else MeasurementSettingsRevision.from_mapping(prior)
    return MeasurementSettingsRevision(selected.predecessor_node_id, 1 if previous is None else previous.revision + 1,
        settings, None if previous is None else previous.supervised_reference_predecessor)


def correction_domain_outlines(authority, snapshot, state_store):
    """Read the accepted analysis-domain outlines already used for segmentation."""
    from .batch_segmentation_artifacts import decode_committed_segmentation_result
    from .region_review_geometry import saved_region_review
    from mif_pipeline.datasets.geometry import polygon_wkb
    selected = decode_committed_segmentation_result(authority)
    sources = {item.artifact_ref.artifact_ref_sha256: item for item in snapshot.committed_artifact_authorities}
    identities = {item.analysis_domain_id for item in selected.analysis_domain_bindings}
    result = {}
    for digest in selected.accepted_analysis_domain_ref_sha256s:
        review = saved_region_review(state_store, sources[digest], snapshot)
        for row in review["regions"]:
            if row["layer"] == "analysis" and row["region_id"] in identities:
                result[row["region_id"]] = polygon_wkb(row["geometry"])[0].hex()
    if set(result) != identities:
        raise BatchArtifactError("The accepted segmentation has incomplete analysis-area outlines")
    return result


def read_correction_submission(state_store, selected):
    """Reopen the exact prepared native return named by the reviewed request."""
    from .cluster.cluster_annotations import _read, _encoded
    folder = state_store.layout.root / "cluster-reviews" / "annotations" / selected.ticket_id
    body = _read(folder / "correction.json")
    if hashlib.sha256(_encoded(body)).hexdigest() != selected.submission_sha256:
        raise BatchArtifactError("The prepared cell correction changed. Return to its review.")
    operations = ([dict(cell_id=body.get("scope", {}).get("cell_id"), operation=body.get("scope", {}).get("operation"))]
                  if selected._v1 else body.get("scope", {}).get("operations"))
    if (body.get("schema_version") != ("mif-known-cell-correction/v1" if selected._v1 else "mif-known-cell-correction/v2")
            or body.get("ticket_id") != selected.ticket_id
            or body.get("predecessor_node_id") != selected.predecessor_node_id
            or body.get("segmentation_result_ref_sha256") != selected.segmentation_result_ref_sha256
            or body.get("previous_correction_ref_sha256") != selected.previous_correction_ref_sha256
            or correction_operations(operations) != selected.operations):
        raise BatchArtifactError("The cell correction differs from its reviewed native draft")
    return body


def _correction_invocation(context, target, state_store):
    from .saved_segmentation_cells import segmentation_cells
    current = state_store.authenticate_current_execution_context(context)
    node = state_store._load_plan_snapshot(current.active_plan_sha256).node_by_id[context.node_id]
    selected = CellCorrectionRequest.from_mapping(node["cell_correction"])
    inputs = {item.artifact_kind: item for item in context.inputs}
    if (current.node_kind != KIND or len(inputs) != len(context.inputs)
            or set(inputs) != {"segmentation_result", *({OUTPUT} if selected.previous_correction_ref_sha256 else ())}
            or inputs["segmentation_result"].artifact_ref_sha256 != selected.segmentation_result_ref_sha256
            or current.project_entry_ids != (target.project_entry_id,)
            or current.participant_ids != (target.sample_id,)):
        raise BatchArtifactError("The cell correction differs from its admitted population")
    base_authority = state_store.authenticate_available_artifact(inputs["segmentation_result"])
    base = segmentation_cells(base_authority, state_store)
    previous = None
    if OUTPUT in inputs:
        if inputs[OUTPUT].artifact_ref_sha256 != selected.previous_correction_ref_sha256:
            raise BatchArtifactError("The cell correction changed its preceding checkpoint")
        previous = decode_committed_cell_correction(
            state_store.authenticate_available_artifact(inputs[OUTPUT]), inputs["segmentation_result"])
    submission = read_correction_submission(state_store, selected)
    previous = correction_source_selection(state_store, selected, submission, inputs['segmentation_result'], previous)
    scope = submission["scope"]
    if (scope["cell_evidence_sha256"] != base.summary.sha256
            or scope["segmentation_population_id"] != base.description["scope"]["population_id"]
            or scope["section_id"] != base.description["scope"]["section_id"]
            or scope["geometry_revision"] != (0 if previous is None else previous["geometry_revision"])
            or scope.get("cell_edits") != (None if previous is None or previous['cell_edits'] is None else dict(previous["cell_edits"]))):
        raise BatchArtifactError("The prepared cell correction changed its selected base or checkpoint")
    if any(item.operation != "delete" for item in selected.operations):
        snapshot = state_store.reopen_execution_snapshot(expected_head_sha256=current.state_head_sha256)
        if (scope["union_geometry_sha256"] != base.description["scope"]["union_geometry_sha256"]
                or scope["domains"] != correction_domain_outlines(base_authority, snapshot, state_store)):
            raise BatchArtifactError("The prepared correction changed its accepted analysis areas")
    body = {"schema_version": "roi-workflow-invocation/v1", "stage": KIND,
            "run_id": target.pipeline_run_id, "slide_id": target.slide_id, "sample_id": target.sample_id,
            "correction_request": selected.to_mapping(), "scope": scope, "outline": submission["outline"]}
    return current, selected, submission, base_authority, body


def validate_cell_correction(evidence, *, state_store, acceptance_context=None):
    """Validate the saved edit, including publication of a completed native Retry."""
    from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
    from mif_pipeline.pipeline_core.stage_result import StageStatus
    from .batch_artifacts import ArtifactOrigin, BatchArtifactRef, BatchNodeExecutionResult
    from .saved_segmentation_cells import segmentation_cells


    context = evidence.context if acceptance_context is None else acceptance_context
    if acceptance_context is not None and (
            context.authorization.failed_retry is None
            or context.node_id != evidence.context.node_id
            or context.inputs != evidence.context.inputs
            or context.authorization.retry_ordinal != evidence.context.authorization.retry_ordinal + 1):
        raise BatchArtifactError("Cell correction recovery differs from its failed native attempt")
    current, selected, submission, base_authority, projected = _correction_invocation(
        context, evidence.authorized, state_store)
    validation = evidence.reopen_validation_inputs()
    expected = canonical_json_bytes(projected)
    if (hashlib.sha256(expected).hexdigest() != validation.scientific_request_body_sha256
            or len(expected) != validation.scientific_request_body_byte_count):
        raise BatchArtifactError("Cell correction request changed from its admitted draft")
    if evidence.wire_stage_result.status is not StageStatus.COMPLETED:
        return BatchNodeExecutionResult(context.authorization.authorization_sha256, ())
    output = evidence.wire_stage_result.outputs
    reviewed_output = evidence.wire_stage_result.to_mapping()["outputs"]
    preview = submission["preview"]
    if selected._v1:
        change_keys = ("cell_id", "operation", "before", "after")
        changed_count, deleted_count = 1, int(selected.operation == "delete")
    else:
        change_keys = ("changes", "unchanged_cell_ids")
        changed_count = len(preview["changes"])
        deleted_count = sum(item["after"] is None for item in preview["changes"])
        if not changed_count or len({item["cell_id"] for item in preview["changes"]}) != changed_count:
            raise BatchArtifactError("The reviewed correction must identify each changed cell once")
    if (not isinstance(output, Mapping) or set(output) != {*change_keys, "cell_edits", "geometry_revision", "cell_count"}
            or any(reviewed_output[key] != preview[key] for key in change_keys)
            or output["geometry_revision"] != preview["geometry_revision"] + changed_count
            or output["cell_count"] != preview["cell_count"] - deleted_count + (
                0 if selected._v1 else sum(item["before"] is None for item in preview["changes"]))
            or evidence.observation.saved_state_disposition != "authenticated_changed"
            or evidence.generation_manifest.get("object_outputs") != output
            or tuple(evidence.generation_manifest.get("generations", ())) != ()
            or tuple(evidence.generation_manifest.get("generation_ids", ())) != (evidence.wire_stage_result.generation_id,)):
        raise BatchArtifactError("Saved correction differs from the reviewed cell change")
    base = segmentation_cells(base_authority, state_store)
    corrected = base.with_edits(dict(output["cell_edits"]))
    try:
        if (corrected.row_count != output["cell_count"]
                or corrected._edit_index.description["geometry_revision"] != output["geometry_revision"]):
            raise BatchArtifactError("Saved correction checkpoint differs from its completed result")
    finally:
        corrected._edit_index.close()
    contract = correction_result_identity_contract(v1=selected._v1)
    identity = dict(node_id=context.node_id, attempt_id=context.authorization.attempt_id,
        section_id=submission["scope"]["section_id"],
        segmentation_population_id=submission["scope"]["segmentation_population_id"],
        segmentation_result_ref_sha256=selected.segmentation_result_ref_sha256,
        previous_correction_ref_sha256=selected.previous_correction_ref_sha256,
        correction_request=_correction_request_identity(selected), cell_edits=dict(output["cell_edits"]),
        geometry_revision=output["geometry_revision"], cell_count=output["cell_count"],
        execution_exchange_sha256=evidence.execution_exchange_sha256,
        validated_exchange_sha256=evidence.validated_exchange_sha256)
    artifact = BatchArtifactRef(
        artifact_id="cell-correction-" + context.node_id[5:], artifact_kind=OUTPUT,
        schema_name=contract.schema_name, schema_version=contract.schema_version,
        origin=ArtifactOrigin.INTERNAL_NODE, producer_node_id=context.node_id,
        producer_attempt_id=context.authorization.attempt_id,
        producer_attempt_authorization_sha256=context.authorization.authorization_sha256,
        external_declaration_sha256=None, external_validation_receipt_sha256=None,
        locator=None, sha256=None, byte_count=None, scope=base_authority.artifact_ref.scope,
        owning_identity=identity, identity_contract=contract,
        owning_identity_contract_sha256=contract.contract_sha256,
        producer_version="mif-pipeline-cell-correction/" + ("v1" if selected._v1 else "v2"), runtime_versions=base_authority.artifact_ref.runtime_versions,
        compatibility_signature_sha256=context.compatibility_signature_sha256,
        configuration_bindings=current.configuration_bindings)
    return BatchNodeExecutionResult(context.authorization.authorization_sha256, (artifact,))


def build_cell_correction_codec(spec, *, state_store):
    from mif_pipeline.qupath_runtime.scripts import QuPathScriptAsset
    from .project_stage_codecs import ProjectStageCodec, ProjectStageCodecBinding

    def body(context, target):
        return _correction_invocation(context, target, state_store)[-1]

    def validate(evidence):
        return validate_cell_correction(evidence, state_store=state_store)

    return ProjectStageCodec.for_stage_owner(
        binding=ProjectStageCodecBinding.create(adapter_spec_sha256=spec.spec_sha256,
            target_script_asset_id=QuPathScriptAsset.CORRECT_KNOWN_CELL.value,
            wire_stage_id=KIND, terminal_stage_id=KIND, request_codec_version="1"),
        spec=spec, request_projector=body, result_validator=validate)
