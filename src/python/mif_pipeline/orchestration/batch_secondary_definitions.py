"""Bind a stage-authored secondary definition set to its accepted group inputs.

Configured definitions remain part of the original analysis settings. A null
definition field is resolved only by a current, committed selection; this owner
does not replace a sample's configuration or its digest.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from types import MappingProxyType

from mif_pipeline.pipeline_core.config import (
    AnalysisConfig, ClassifierMode, SecondaryStructureSpec,
)
from mif_pipeline.pipeline_core.durable_records import (
    DurableRecordError, canonical_json_bytes, publish_immutable_record,
    read_canonical_record,
)

from .batch_analysis_preflight import MIF_PIPELINE_BATCH_RUNTIME_VERSION
from .batch_artifacts import (
    ArtifactConfigurationBinding, ArtifactIdentityContract, ArtifactOrigin, ArtifactScope,
    BatchArtifactError, BatchArtifactRef, BatchCommittedArtifactAuthority,
    validate_artifact_ref, validate_committed_artifact_authority_set,
)
from .batch_manifest import ResolvedBatchSample, ReusableArtifactDeclaration

NODE_KIND = "secondary_structure_definition"
ARTIFACT_KIND = "secondary_definition_selection"
SCHEMA_NAME = "mif-secondary-definition-selection"
CONTEXT_SCHEMA = "secondary-definition-review/v1"
RESPONSE_SCHEMA = "secondary-definition-choice/v1"
PRODUCER_VERSION = "mif-pipeline-secondary-definitions/v1"
RESPONSE_VALIDATOR_ID = "mif-secondary-definition-choice"

_IDENTITY_FIELDS = (
    "selection_sha256", "source_ref_sha256s", "interaction_response_sha256",
    "interaction_context_sha256", "body_sha256", "body_byte_count",
)
_MATERIAL_FIELDS = (*_IDENTITY_FIELDS[:-2], "selection_context", "secondary_structures",
                    "profile_declarations")
_MAX_SELECTION_BYTES = 4 * 1024 * 1024
_CONTEXT_FIELDS = frozenset({
    "schema_version", "batch_id", "validation_domain_id", "participant_ids",
    "configuration_bindings", "analysis_domain_bindings", "project_entry_ids",
    "validation_config", "source_ref_sha256s", "eligible_primary_class_ids",
})


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(_plain(value))).hexdigest()


def secondary_definition_identity_contract() -> ArtifactIdentityContract:
    return ArtifactIdentityContract(
        contract_id="mif-secondary-definition-selection-identity/v1",
        artifact_kind=ARTIFACT_KIND, schema_name=SCHEMA_NAME, schema_version=1,
        required_fields=_IDENTITY_FIELDS, optional_fields=(),
        required_runtime_names=("mif_pipeline",),
    )


def validate_definition_context(value: object) -> Mapping[str, object]:
    """Check the closed request before using its current parent/channel inventory."""
    from .batch_artifacts import AnalysisDomainBinding

    if (not isinstance(value, Mapping)
            or not _CONTEXT_FIELDS <= set(value)
            or set(value) - _CONTEXT_FIELDS - {"initial_secondary_structures", "profile_directory", "cell_selector_inventory", "marker_review_scope"}
            or value["schema_version"] != CONTEXT_SCHEMA):
        raise BatchArtifactError("Refresh the secondary structure definition review.")
    if "profile_directory" in value:
        directory = value["profile_directory"]
        if not isinstance(directory, str) or not Path(directory).is_absolute():
            raise BatchArtifactError("Refresh the secondary structure definition review.")
    for name in ("batch_id", "validation_domain_id"):
        item = value[name]
        if type(item) is not str or not item or item != item.strip():
            raise BatchArtifactError(f"Secondary definition {name} is invalid")
    participants = value["participant_ids"]
    entries = value["project_entry_ids"]
    sources = value["source_ref_sha256s"]
    for name, items in (("participants", participants), ("project entries", entries), ("sources", sources)):
        if (not isinstance(items, (tuple, list)) or not items
                or any(type(item) is not str or not item for item in items)
                or list(items) != sorted(set(items))):
            raise BatchArtifactError(f"Secondary definition {name} are not exact")
    if any(len(item) != 64 or set(item) - set("0123456789abcdef") for item in sources):
        raise BatchArtifactError("Secondary definition source digests are invalid")
    configurations = tuple(ArtifactConfigurationBinding.from_mapping(item)
                           for item in value["configuration_bindings"])
    bindings = tuple(AnalysisDomainBinding.from_mapping(item)
                     for item in value["analysis_domain_bindings"])
    if (tuple(item.sample_id for item in configurations) != tuple(participants)
            or not bindings or len(set(bindings)) != len(bindings)
            or {item.sample_id for item in bindings} != set(participants)
            or {item.project_entry_id for item in bindings} != set(entries)):
        raise BatchArtifactError("Secondary definition group or region coverage differs")
    config = AnalysisConfig.from_mapping(value["validation_config"])
    if config.secondary_structures is not None or config.channels is None:
        raise BatchArtifactError("Secondary definitions require the accepted channels")
    parents = value["eligible_primary_class_ids"]
    if (not isinstance(parents, (tuple, list)) or list(parents) != sorted(set(parents))
            or not set(parents) <= {item.class_id for item in config.primary_classes or ()}):
        raise BatchArtifactError("Secondary definition primary parents are not exact")
    if "initial_secondary_structures" in value:
        initial = value["initial_secondary_structures"]
        if not isinstance(initial, (tuple, list)):
            raise BatchArtifactError("Previous secondary definitions are not a structure list")
        for index, item in enumerate(initial):
            SecondaryStructureSpec.from_mapping(item, field=f"initial_secondary_structures[{index}]")
    if "cell_selector_inventory" in value:
        _validate_cell_selector_inventory(value["cell_selector_inventory"])
    if "marker_review_scope" in value:
        _validate_marker_review_scope(value["marker_review_scope"], value)
    return value


def _validate_marker_review_scope(scope, context):
    """Check compact source and section identities without reading cell rows."""
    if (not isinstance(scope, Mapping) or not {"source_ref_sha256s", "sections"} <= set(scope)
            or set(scope) - {"source_ref_sha256s", "sections", "marker_measurement_ids"}):
        raise BatchArtifactError("Refresh the secondary marker review source scope.")
    refs = scope["source_ref_sha256s"]
    if (not isinstance(refs, (tuple, list)) or not refs
            or any(not isinstance(ref, str) or len(ref) != 64
                   or set(ref) - set("0123456789abcdef") for ref in refs)
            or list(refs) != sorted(set(refs))
            or not set(refs) <= set(context["source_ref_sha256s"])):
        raise BatchArtifactError("Secondary marker review sources differ from the saved results.")
    sections = scope["sections"]
    if (not isinstance(sections, (tuple, list)) or not sections
            or any(not isinstance(item, Mapping) or set(item) != {"slide_id", "section_id"}
                   or any(not isinstance(item[field], str) or not item[field].strip()
                          for field in ("slide_id", "section_id")) for item in sections)):
        raise BatchArtifactError("Secondary marker review requires exact image and section identities.")
    identities = [(item["slide_id"], item["section_id"]) for item in sections]
    if (identities != sorted(set(identities))
            or {item[0] for item in identities} != {item["slide_id"] for item in context["analysis_domain_bindings"]}):
        raise BatchArtifactError("Secondary marker review section identities repeat or differ from this group.")
    if "marker_measurement_ids" in scope:
        names = scope["marker_measurement_ids"]
        if (not isinstance(names, (list, tuple))
                or any(not isinstance(name, str) or not name.strip() for name in names)
                or list(names) != sorted(set(names))):
            raise BatchArtifactError("Secondary marker measurements must name distinct saved intensity columns.")
        inventory = context.get("cell_selector_inventory")
        if inventory is not None and not set(names) <= set(inventory["measurement_ids"]):
            raise BatchArtifactError("Secondary marker measurements differ from the saved cell inventory.")


def _parse_definition_choice(
    value: object, context: Mapping[str, object],
) -> tuple[SecondaryStructureSpec, ...]:
    """Decode typed values; source files are checked at public acceptance below."""
    context = validate_definition_context(context)
    if (not isinstance(value, Mapping)
            or set(value) != {"schema_version", "secondary_structures"}
            or value["schema_version"] != RESPONSE_SCHEMA):
        raise BatchArtifactError("Choose the secondary structures for this review.")
    raw = value["secondary_structures"]
    if not isinstance(raw, (tuple, list)):
        raise BatchArtifactError("Choose no secondary structures or supply their definitions.")
    structures = tuple(SecondaryStructureSpec.from_mapping(item, field=f"secondary_structures[{i}]")
                       for i, item in enumerate(raw))
    candidate = _plain(context["validation_config"])
    candidate["secondary_structures"] = [item.to_mapping() for item in structures]
    candidate["semantic"]["profile_references"] = [item.semantic_profile.to_mapping() for item in structures]
    # Use the existing parent, channel, profile and count validation as a pure
    # check. This temporary config is never assigned to a manifest or hashed as
    # the analysis's admitted settings.
    AnalysisConfig.from_mapping(candidate)
    allowed_parents = {"AnalysisDomain", *context["eligible_primary_class_ids"]}
    for structure in structures:
        if not set(structure.eligible_parent_ids) <= allowed_parents:
            raise BatchArtifactError("Choose parents available in every image in this processing group.")
        classifier = structure.classifier
        if (classifier.mode is not ClassifierMode.TRAIN_NEW
                or classifier.validation_domain_id != context["validation_domain_id"]):
            raise BatchArtifactError("A new secondary definition must train within this processing group.")
        if not structure.semantic_profile.artifact_path.is_absolute():
            raise BatchArtifactError("Choose a complete criterion profile file location.")
    return structures


def _profile_declaration_id(path, source_sha256, validation_domain_id):
    return "semantic-profile-" + _digest({
        "source_path": str(path.resolve(strict=False)),
        "source_sha256": source_sha256,
        "validation_domain_id": validation_domain_id,
    })


def pending_group_profile_declarations(manifest, validation_domain_id):
    """Select this interactive group's authored profiles, preserving configured users."""
    samples = tuple(item for item in manifest.enabled_samples
                    if item.validation_domain_id == validation_domain_id)
    if not any(item.config.secondary_structures is None for item in samples):
        return ()
    configured = {
        ((reference.artifact_path if reference.artifact_path.is_absolute()
          else sample.config.output_dir / reference.artifact_path).resolve(strict=False),
         reference.content_sha256)
        for sample in samples for structure in (sample.config.secondary_structures or ())
        for reference in (structure.semantic_profile,)
    }
    return tuple(item for item in manifest.manifest.reusable_artifacts
                 if item.artifact_kind == "semantic_profile"
                 and item.validation_domain_id == validation_domain_id
                 and item.artifact_id == _profile_declaration_id(item.locator, item.sha256, validation_domain_id)
                 and (item.locator.resolve(strict=False), item.sha256) not in configured)


def _prepare_authored_profiles(value, context):
    """Turn in-app criteria into ordinary profile references without writing files."""
    from mif_pipeline.roi_validation import SemanticProfileV3
    from .batch_phenotyping_contracts import MAX_ROI_SEMANTIC_PROFILE_BYTES

    validate_definition_context(context)
    prepared = _plain(value)
    authored = {}
    if not isinstance(prepared, dict) or not isinstance(
        prepared.get("secondary_structures"), list
    ):
        return prepared, authored
    for row in prepared["secondary_structures"]:
        if not isinstance(row, dict):
            continue  # The ordinary definition parser reports the malformed row.
        raw = row.get("semantic_profile")
        if not isinstance(raw, Mapping) or raw.get("schema_version") not in {"semantic-profile/v3", "semantic-profile/v4"}:
            continue
        profile = SemanticProfileV3.from_mapping(raw)
        if profile.structure_id != row.get("structure_id"):
            raise BatchArtifactError("The criteria belong to a different structure.")
        data = canonical_json_bytes(profile.to_mapping())
        if len(data) > MAX_ROI_SEMANTIC_PROFILE_BYTES:
            raise BatchArtifactError("The structure criteria exceed the supported file size.")
        digest = hashlib.sha256(data).hexdigest()
        if "profile_directory" not in context:
            raise BatchArtifactError("Reopen the structure definition to save its criteria.")
        path = Path(context["profile_directory"]) / f"{digest}.json"
        row["semantic_profile"] = {
            "profile_id": profile.profile_id,
            "profile_version": profile.profile_version,
            "content_sha256": digest,
            "artifact_path": str(path),
        }
        authored[profile.structure_id] = (data, profile)
    return prepared, authored


def _validate_population_marker_sources(value, context):
    """Keep locally reviewed thresholds tied to this definition's saved cells."""
    from mif_pipeline.roi_validation.population_marker import parse_population_marker_gate
    if isinstance(value, Mapping):
        if value.get('type') == 'population_marker_fraction':
            selector = value['selector']
            gate = parse_population_marker_gate(selector['positivity_gate'], selector['measurement_id'])
            scope = context.get('marker_review_scope')
            if scope is None:
                raise ValueError('Refresh the secondary structure review before adding a population marker measurement.')
            _validate_marker_review_scope(scope, context)
            if ("marker_measurement_ids" in scope
                    and selector['measurement_id'] not in scope['marker_measurement_ids']):
                raise ValueError('Choose a saved marker measurement available in every included image.')
            refs = set(gate['source_ref_sha256s'])
            if (not set(scope['source_ref_sha256s']) <= refs
                    or not refs <= set(context['source_ref_sha256s'])):
                raise ValueError('The marker threshold belongs to different saved results. Review it using the current cell measurements.')
            sections = {(item['slide_id'], item['section_id']) for item in scope['sections']}
            if {(item['slide_id'], item['section_id']) for item in gate['decisions']} != sections:
                raise ValueError('Review a marker threshold for each exact image and section in this processing group.')
        for child in value.values():
            _validate_population_marker_sources(child, context)
    elif isinstance(value, (tuple, list)):
        for child in value:
            _validate_population_marker_sources(child, context)


def _accepted_choice_material(value, context, *, materialize=False):
    """Validate imported or authored criteria; publish drafts only during execution."""
    from .batch_semantic_profile_import import (
        read_semantic_profile_source, semantic_profile_compatibility_sha256,
    )
    from .batch_phenotyping_contracts import semantic_profile_identity_contract
    from .analysis_services import validate_semantic_profile_selectors

    prepared, authored = _prepare_authored_profiles(value, context)
    structures = _parse_definition_choice(prepared, context)
    selector_config = AnalysisConfig.from_mapping(context["validation_config"])
    declarations = []
    contract = semantic_profile_identity_contract()
    for structure in structures:
        reference = structure.semantic_profile
        data, profile = (
            authored[structure.structure_id]
            if structure.structure_id in authored
            else read_semantic_profile_source(reference.artifact_path)
        )
        if (hashlib.sha256(data).hexdigest() != reference.content_sha256
                or (profile.profile_id, profile.profile_version, profile.structure_id)
                != (reference.profile_id, reference.profile_version, structure.structure_id)):
            raise BatchArtifactError("The criterion profile changed or belongs to another structure.")
        if profile.schema_version != "semantic-profile/v4":
            raise BatchArtifactError("Edit the previous criterion profile to use the current region review before accepting it.")
        validate_semantic_profile_selectors(
            profile, selector_config,
            secondary_structures=structures,
            defer_cell_selectors="cell_selector_inventory" not in context,
            cell_selector_inventory=context.get("cell_selector_inventory"),
        )
        _validate_population_marker_sources(profile.to_mapping(), context)
        declarations.append(ReusableArtifactDeclaration(
            artifact_id=_profile_declaration_id(reference.artifact_path, reference.content_sha256,
                                                context["validation_domain_id"]),
            artifact_kind=contract.artifact_kind, schema_name=contract.schema_name,
            schema_version=str(contract.schema_version),
            locator=reference.artifact_path.resolve(strict=False),
            sha256=reference.content_sha256, byte_count=len(data),
            owning_identity={"profile_id": profile.profile_id,
                             "profile_version": profile.profile_version,
                             "structure_id": profile.structure_id},
            runtime_versions={"mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION},
            validation_domain_id=context["validation_domain_id"],
            compatibility_signature_sha256=semantic_profile_compatibility_sha256(profile),
        ))
    if materialize:
        from .batch_phenotyping_contracts import MAX_ROI_SEMANTIC_PROFILE_BYTES

        # Validate the entire choice before creating any profile. The same
        # content uses the same file when an interrupted execution is retried.
        for structure in structures:
            if structure.structure_id in authored:
                publish_immutable_record(
                    structure.semantic_profile.artifact_path,
                    authored[structure.structure_id][0],
                    max_bytes=MAX_ROI_SEMANTIC_PROFILE_BYTES,
                    label="secondary structure criteria",
                )
    return structures, tuple(declarations)


def validate_secondary_definition_choice(
    value: object, context: Mapping[str, object],
) -> tuple[SecondaryStructureSpec, ...]:
    """Validate criteria and selectors without saving an in-app draft."""
    return _accepted_choice_material(value, context)[0]


@dataclass(frozen=True, slots=True, init=False)
class ValidatedSecondaryDefinitionSelection:
    """A read-only choice issued only by the committed artifact decoder."""

    artifact_ref: BatchArtifactRef
    state_head_sha256: str
    active_plan_sha256: str
    terminal_record_sha256: str
    structures: tuple[SecondaryStructureSpec, ...]
    profile_declarations: tuple[ReusableArtifactDeclaration, ...]
    context: Mapping[str, object]

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("Secondary definition selections require committed state authority")


def decode_committed_secondary_definition_selection(
    authority: BatchCommittedArtifactAuthority, *, state_store,
) -> ValidatedSecondaryDefinitionSelection:
    from .batch_state import BatchStateStore

    if type(authority) is not BatchCommittedArtifactAuthority or type(state_store) is not BatchStateStore:
        raise BatchArtifactError("Secondary definitions require committed state authority")
    validate_committed_artifact_authority_set((authority,))
    if authority.producer_node_kind != NODE_KIND or authority.execution_exchange_sha256 is not None:
        raise BatchArtifactError("Secondary definitions require their exact completed owner")
    ref = authority.artifact_ref
    context, structures, declarations = _decode_definition_material(
        ref, record_directory=state_store.layout.state / "secondary-definitions")
    result = object.__new__(ValidatedSecondaryDefinitionSelection)
    for name, value in dict(artifact_ref=ref, state_head_sha256=authority.state_head_sha256,
                            active_plan_sha256=authority.active_plan_sha256,
                            terminal_record_sha256=authority.terminal_record_sha256,
                            structures=structures, profile_declarations=declarations, context=context).items():
        object.__setattr__(result, name, value)
    return result


def _definition_record_identity(ref):
    """Validate the compact identity before selecting its relative control file."""
    ref = validate_artifact_ref(ref)
    contract = secondary_definition_identity_contract()
    if (ref.artifact_kind != ARTIFACT_KIND
            or ref.identity_contract != contract or ref.owning_identity_contract_sha256 != contract.contract_sha256
            or (ref.schema_name, ref.schema_version) != (SCHEMA_NAME, 1)
            or ref.origin is not ArtifactOrigin.INTERNAL_NODE or ref.producer_version != PRODUCER_VERSION
            or dict(ref.runtime_versions) != {"mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION}
            or ref.locator is not None or ref.sha256 is not None or ref.byte_count is not None):
        raise BatchArtifactError("Secondary definition selection lacks its owner contract")
    digest = ref.owning_identity["body_sha256"]
    size = ref.owning_identity["body_byte_count"]
    if (type(digest) is not str or len(digest) != 64 or set(digest) - set("0123456789abcdef")
            or type(size) is not int or not 0 < size <= _MAX_SELECTION_BYTES):
        raise BatchArtifactError("Secondary definition record identity is invalid")
    return digest, size


def _read_definition_record(ref, record_directory):
    digest, size = _definition_record_identity(ref)
    try:
        payload, record = read_canonical_record(
            record_directory / f"{digest}.json", max_bytes=_MAX_SELECTION_BYTES,
            label="secondary definition selection",
        )
    except (OSError, DurableRecordError) as exc:
        raise BatchArtifactError("The saved secondary definition selection is unavailable") from exc
    if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
        raise BatchArtifactError("The saved secondary definition record changed")
    return payload, record


def _decode_definition_material(ref, *, record_directory=None, record_payload=None):
    """Decode a committed artifact or authenticated historical/capsule record."""
    if record_payload is None:
        if record_directory is None:
            raise TypeError("Secondary definition decoding requires its current record directory")
        _payload, record = _read_definition_record(ref, record_directory)
    else:
        digest, size = _definition_record_identity(ref)
        if (type(record_payload) is not bytes or len(record_payload) != size
                or hashlib.sha256(record_payload).hexdigest() != digest):
            raise BatchArtifactError("The imported secondary definition record changed")
        try:
            record = json.loads(record_payload)
        except (ValueError, UnicodeDecodeError) as exc:
            raise BatchArtifactError("The imported secondary definition record is invalid") from exc
    if (not isinstance(record, Mapping) or set(record) != {"schema_name", "schema_version", "selection"}
            or record["schema_name"] != SCHEMA_NAME or record["schema_version"] != 1):
        raise BatchArtifactError("The saved secondary definition record changed")
    identity = record["selection"]
    if (not isinstance(identity, Mapping) or set(identity) != set(_MATERIAL_FIELDS)
            or any(_plain(ref.owning_identity[name]) != identity[name] for name in _IDENTITY_FIELDS[:-2])):
        raise BatchArtifactError("Secondary definition selection has a foreign schema")
    context = identity["selection_context"]
    structures = _parse_definition_choice(
        {"schema_version": RESPONSE_SCHEMA, "secondary_structures": identity["secondary_structures"]},
        context,
    )
    declarations = tuple(ReusableArtifactDeclaration.from_mapping(item, index=index)
                         for index, item in enumerate(identity["profile_declarations"]))
    if len(declarations) != len(structures):
        raise BatchArtifactError("Secondary definitions lack their exact criterion declarations")
    for structure, declaration in zip(structures, declarations, strict=True):
        reference = structure.semantic_profile
        if (declaration.artifact_id != _profile_declaration_id(reference.artifact_path,
                reference.content_sha256, context["validation_domain_id"])
                or (declaration.artifact_kind, declaration.schema_name)
                != ("semantic_profile", "semantic-profile")
                or declaration.schema_version not in {"3", "4"}
                or declaration.locator != reference.artifact_path.resolve(strict=False)
                or declaration.sha256 != reference.content_sha256
                or dict(declaration.owning_identity) != {"profile_id": reference.profile_id,
                    "profile_version": reference.profile_version, "structure_id": structure.structure_id}
                or declaration.validation_domain_id != context["validation_domain_id"]
                or declaration.target_sample_ids or declaration.primary_settings is not None
                or dict(declaration.runtime_versions) != {"mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION}):
            raise BatchArtifactError("Criterion declaration differs from the accepted definition")
    digest = _digest({name: value for name, value in identity.items() if name != "selection_sha256"})
    from .batch_artifacts import AnalysisDomainBinding

    if (identity["selection_sha256"] != digest
            or ref.artifact_id != "secondary-definitions-" + digest[:48]
            or identity["interaction_context_sha256"] != _digest(context)
            or tuple(identity["source_ref_sha256s"]) != tuple(context["source_ref_sha256s"])
            or ref.configuration_bindings != tuple(ArtifactConfigurationBinding.from_mapping(item)
                                                   for item in context["configuration_bindings"])
            or ref.scope != ArtifactScope(
                context["validation_domain_id"], None, None, None,
                tuple(AnalysisDomainBinding.from_mapping(item)
                      for item in context["analysis_domain_bindings"]),
            )):
        raise BatchArtifactError("Secondary definition selection scope or content changed")
    response_sha = identity["interaction_response_sha256"]
    if type(response_sha) is not str or len(response_sha) != 64 or set(response_sha) - set("0123456789abcdef"):
        raise BatchArtifactError("Secondary definition selection lacks its accepted response")
    return context, structures, declarations


def resolve_secondary_structures(
    sample: ResolvedBatchSample,
    selection: ValidatedSecondaryDefinitionSelection | None = None,
) -> tuple[SecondaryStructureSpec, ...]:
    """Return configured or accepted definitions; never treat a pending choice as none."""
    if type(sample) is not ResolvedBatchSample:
        raise TypeError("Secondary definitions require a resolved sample")
    if not sample.enabled:
        raise BatchArtifactError("Secondary definitions require an enabled sample")
    configured = sample.config.secondary_structures
    if configured is not None:
        if selection is not None:
            raise BatchArtifactError("A configured secondary definition cannot be replaced by an initial choice")
        return configured
    if type(selection) is not ValidatedSecondaryDefinitionSelection:
        raise BatchArtifactError("Choose the secondary structures before continuing this stage.")
    context = selection.context
    expected = ArtifactConfigurationBinding(sample.sample_id, sample.resolved_config_sha256)
    if (sample.validation_domain_id != context["validation_domain_id"]
            or sample.sample_id not in context["participant_ids"]
            or expected not in selection.artifact_ref.configuration_bindings):
        raise BatchArtifactError("Secondary definitions belong to another sample or processing group")
    return selection.structures


def secondary_definition_selections(snapshot, *, state_store, participant_ids, node_id=None):
    """Reopen each pending sample's choice from this exact execution snapshot.

    An executing consumer must descend from the accepted definition node. Pure
    publication/planning callers can omit the consumer while retaining the same
    current-head, group and original-configuration checks.
    """
    from .batch_state import BatchExecutionSnapshot

    if type(snapshot) is not BatchExecutionSnapshot:
        raise TypeError("Secondary definitions require the current execution snapshot")
    selected_ids = set(participant_ids)
    samples = {item.sample_id: item for item in snapshot.manifest.enabled_samples
               if item.sample_id in selected_ids}
    if set(samples) != selected_ids:
        raise BatchArtifactError("Secondary definition participants are not current enabled images")
    pending_ids = {key for key, sample in samples.items() if sample.config.secondary_structures is None}
    if not pending_ids:
        return MappingProxyType({})
    ancestors = None
    if node_id is not None:
        nodes = {item.node_id: item for item in snapshot.plan.nodes}
        if node_id not in nodes:
            raise BatchArtifactError("Secondary definition consumer is outside the current plan")
        ancestors = set()
        frontier = list(nodes[node_id].dependency_ids)
        while frontier:
            key = frontier.pop()
            if key not in ancestors:
                ancestors.add(key)
                frontier.extend(nodes[key].dependency_ids)
    result = {}
    for authority in snapshot.committed_artifact_authorities:
        ref = authority.artifact_ref
        if (ref.artifact_kind != ARTIFACT_KIND
                or not pending_ids.intersection(item.sample_id for item in ref.configuration_bindings)):
            continue
        selection = decode_committed_secondary_definition_selection(authority, state_store=state_store)
        if (selection.state_head_sha256 != snapshot.head.sha256
                or selection.active_plan_sha256 != snapshot.plan.plan_sha256
                or selection.context["batch_id"] != snapshot.head.batch_id
                or (ancestors is not None and ref.producer_node_id not in ancestors)):
            raise BatchArtifactError("Secondary definitions are not an accepted dependency of this task")
        for sample_id in pending_ids.intersection(selection.context["participant_ids"]):
            if sample_id in result:
                raise BatchArtifactError("This image has more than one accepted secondary definition choice")
            resolve_secondary_structures(samples[sample_id], selection)
            result[sample_id] = selection
    if set(result) != pending_ids:
        raise BatchArtifactError("Choose the secondary structures before continuing this stage.")
    return MappingProxyType(result)


def manifest_for_secondary_definitions(manifest, selection):
    """Select this group's criterion declarations while retaining original image settings."""
    if type(selection) is not ValidatedSecondaryDefinitionSelection:
        raise TypeError("Profile publication requires an accepted secondary definition choice")
    return _manifest_for_definition_material(manifest, selection.context,
                                             selection.profile_declarations)


def _manifest_for_definition_material(manifest, context, selected_declarations):
    """Reproduce publication from authenticated current or historical metadata."""
    from .batch_manifest import revalidate_retained_manifest

    participants = set(context["participant_ids"])
    samples = tuple(item for item in manifest.enabled_samples if item.sample_id in participants)
    bindings = tuple(ArtifactConfigurationBinding.from_mapping(item)
                     for item in context["configuration_bindings"])
    if ({item.sample_id for item in samples} != participants
            or context["batch_id"] != manifest.manifest.batch_id
            or any(item.config.secondary_structures is not None
                   or item.validation_domain_id != context["validation_domain_id"]
                   or ArtifactConfigurationBinding(item.sample_id, item.resolved_config_sha256) not in bindings
                   for item in samples)):
        raise BatchArtifactError("Secondary profile targets differ from this analysis")
    replaced = pending_group_profile_declarations(manifest, context["validation_domain_id"])
    pending_group = {item.sample_id for item in manifest.enabled_samples
                     if item.validation_domain_id == context["validation_domain_id"]
                     and item.config.secondary_structures is None}
    if replaced and not pending_group <= participants:
        raise BatchArtifactError("Review every image sharing this group's criterion profiles before replacing them")
    replaced_ids = {item.artifact_id for item in replaced}
    declarations = {item.artifact_id: item for item in manifest.manifest.reusable_artifacts
                    if item.artifact_id not in replaced_ids}
    for declaration in selected_declarations:
        existing = tuple(item for item in declarations.values()
                         if item.artifact_kind == "semantic_profile"
                         and item.locator.resolve(strict=False) == declaration.locator
                         and item.sha256 == declaration.sha256
                         and item.validation_domain_id in (None, context["validation_domain_id"]))
        if existing:
            if (len(existing) != 1 or existing[0].sha256 != declaration.sha256
                    or existing[0].byte_count != declaration.byte_count
                    or existing[0].owning_identity != declaration.owning_identity
                    or existing[0].compatibility_signature_sha256 != declaration.compatibility_signature_sha256):
                raise BatchArtifactError("The selected criterion profile conflicts with an existing declaration")
            continue
        previous = declarations.setdefault(declaration.artifact_id, declaration)
        if previous != declaration:
            raise BatchArtifactError("The accepted criterion profile conflicts with an existing declaration")
    if declarations == {item.artifact_id: item for item in manifest.manifest.reusable_artifacts}:
        return manifest
    value = manifest.manifest.to_mapping()
    if declarations:
        value["reusable_artifacts"] = [item.to_mapping() for item in sorted(declarations.values(), key=lambda item: item.artifact_id)]
    else:
        value.pop("reusable_artifacts", None)
    payload = canonical_json_bytes(value)
    result = revalidate_retained_manifest(payload, payload)
    if {item.sample_id: item.resolved_config_sha256 for item in result.samples} != {
            item.sample_id: item.resolved_config_sha256 for item in manifest.samples}:
        raise BatchArtifactError("Criterion profile publication changed an image configuration")
    return result


def validate_secondary_profile_manifest_transition(previous, current):
    """Identify profile-only changes; the state owner checks the exact group choice."""
    before, after = previous.manifest.to_mapping(), current.manifest.to_mapping()
    old, new = before.pop("reusable_artifacts", []), after.pop("reusable_artifacts", [])
    changed = [item for item in new if item not in old] + [item for item in old if item not in new]
    if (before != after or not changed
            or any(item.get("artifact_kind") != "semantic_profile" for item in changed)
            or {item.sample_id: item.resolved_config_sha256 for item in previous.samples}
            != {item.sample_id: item.resolved_config_sha256 for item in current.samples}):
        raise BatchArtifactError("A secondary choice may only change its criterion profiles with unchanged image settings")


def build_secondary_definition_stage_spec():
    """Reuse the existing group review and input contracts for the new decision."""
    from .batch_artifacts import ArtifactMultiplicity, DispatchMode
    from .batch_training.stage_adapters import _requirement, _spec
    from .batch_analysis_domain_artifacts import accepted_analysis_domain_identity_contract
    from .batch_analysis_preflight import analysis_preflight_identity_contract
    from .batch_project import project_catalogue_entry_identity_contract
    from .batch_classifier_inference_artifacts import primary_classifier_inference_result_identity_contract
    from .batch_training.planning import training_plan_set_identity_contract
    from .batch_measurement_artifacts import measurements_result_identity_contract
    from .batch_phenotyping.marker_application_records import marker_section_application_identity_contract
    from .batch_phenotyping.supervised_records import supervised_phenotype_result_identity_contract
    from .batch_phenotyping.application_codec import cluster_label_import_result_identity_contract

    spec = _spec(
        NODE_KIND,
        (
            _requirement(accepted_analysis_domain_identity_contract(), ArtifactMultiplicity.PER_PARTICIPANT),
            _requirement(analysis_preflight_identity_contract(), ArtifactMultiplicity.PER_PARTICIPANT),
            _requirement(project_catalogue_entry_identity_contract(), ArtifactMultiplicity.PER_PARTICIPANT),
            _requirement(measurements_result_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        ),
        secondary_definition_identity_contract(),
        resource_kind="shared_classifier_mutation", dispatch_mode=DispatchMode.SINGLE,
    )
    return replace(spec, optional_inputs=(
        _requirement(primary_classifier_inference_result_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        _requirement(training_plan_set_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        _requirement(marker_section_application_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        _requirement(supervised_phenotype_result_identity_contract(), ArtifactMultiplicity.EXACT_SET),
        _requirement(cluster_label_import_result_identity_contract(), ArtifactMultiplicity.EXACT_SET),
    ))


def plan_has_secondary_definition_choice(plan) -> bool:
    return (any(item.kind == NODE_KIND for item in plan.nodes)
            or any(item.node_kind == NODE_KIND for item in plan.deferred_nodes))


def saved_secondary_definition(snapshot, node, state_store):
    """Reopen this completed definition with its exact saved structure and profile choices."""
    authorities = [item for item in snapshot.committed_artifact_authorities
                   if item.artifact_ref.artifact_kind == ARTIFACT_KIND
                   and item.artifact_ref.producer_node_id == node.node_id]
    if len(authorities) != 1:
        raise BatchArtifactError("The accepted structure definitions are unavailable. Refresh the analysis.")
    saved = decode_committed_secondary_definition_selection(authorities[0], state_store=state_store)
    return secondary_definition_review({
        **saved.context, "initial_secondary_structures": [row.to_mapping() for row in saved.structures],
    })


def secondary_definition_initial_values(node, state_store):
    """Offer the exact settled predecessor as a draft; never accept it by replay."""
    from mif_pipeline.pipeline_core import StageStatus

    revision = node.settled_review_revision
    if revision is None:
        return None
    attempt = state_store.load_attempt(revision.predecessor_terminal_record_sha256)
    if attempt.node_id != revision.predecessor_node_id or attempt.stage_result.status not in {
            StageStatus.COMPLETED, StageStatus.FAILED}:
        raise BatchArtifactError("Secondary definition revision differs from its retained task")
    if attempt.stage_result.status is StageStatus.FAILED:
        return None
    refs = tuple(ref for ref in attempt.output_artifacts if ref.artifact_kind == ARTIFACT_KIND)
    if (len(refs) != 1 or refs[0].producer_node_id != revision.predecessor_node_id
            or refs[0].configuration_bindings != node.configuration_bindings
            or refs[0].scope.validation_domain_id != node.validation_domain_id):
        raise BatchArtifactError("The previous secondary definition choice belongs to another group")
    _context, structures, _declarations = _decode_definition_material(
        refs[0], record_directory=state_store.layout.state / "secondary-definitions")
    return [item.to_mapping() for item in structures]


def _definition_primary_settings(*, primary, plans, bindings):
    """Require an accepted classification or explicit skip for each region."""
    from mif_pipeline.pipeline_core.config import PrimaryClassifierSettings

    covered = [binding for item in primary for binding in item.analysis_domain_bindings]
    settings = []
    skipped = False
    for projection in plans:
        plan_set = projection.plan_set
        if plan_set.skipped_primary is not None:
            skipped = True
            covered.extend(plan_set.skipped_primary.analysis_domain_bindings)
        elif plan_set.saved_selection is not None:
            settings.append(PrimaryClassifierSettings.from_mapping(
                plan_set.saved_selection.declaration.primary_settings))
        elif len(plan_set.plans) == 1 and plan_set.plans[0].request.primary_settings is not None:
            settings.append(plan_set.plans[0].request.primary_settings)
        else:
            raise BatchArtifactError("Primary classifier settings are not a complete accepted choice")
    if len(set(covered)) != len(covered) or set(covered) != set(bindings):
        raise BatchArtifactError("Choose primary classification or skip it for every analysis region")
    parents = set() if skipped or not settings else set.intersection(
        *({item.class_id for item in value.primary_classes} for value in settings))
    return tuple(settings), parents


def _validate_cell_selector_inventory(value):
    names = ("phenotype_ids", "marker_ids", "measurement_ids", "channel_roles")
    if not isinstance(value, Mapping) or set(value) != {*names, "measurement_channels"}:
        raise BatchArtifactError("Refresh the available cell measurements and phenotypes.")
    for name in names:
        items = value[name]
        if (not isinstance(items, (tuple, list))
                or any(not isinstance(item, str) or not item for item in items)
                or len(items) != len(set(items))):
            raise BatchArtifactError("The available cell measurement or phenotype names are invalid.")
    channels = value["measurement_channels"]
    if (not isinstance(channels, Mapping)
            or not set(channels) <= set(value["measurement_ids"])
            or not (set(channels.values()) - {None}) <= set(value["channel_roles"])):
        raise BatchArtifactError("The saved measurements do not match their image channels.")


def _common_cell_selector_inventory(inventories):
    """Offer quantities that can be evaluated in every section of this group."""
    if not inventories:
        raise BatchArtifactError("Complete cell measurements and phenotyping before choosing secondary structures.")
    for inventory in inventories:
        _validate_cell_selector_inventory(inventory)
    result = {name: sorted(set.intersection(*(set(item[name]) for item in inventories)))
              for name in ("phenotype_ids", "marker_ids", "measurement_ids", "channel_roles")}
    result["measurement_channels"] = {}
    for name in result["measurement_ids"]:
        channels = {item["measurement_channels"].get(name) for item in inventories}
        if len(channels) != 1:
            raise BatchArtifactError(f"The saved measurement {name!r} uses different channels across this processing group.")
        result["measurement_channels"][name] = channels.pop()
    _validate_cell_selector_inventory(result)
    return result


def _definition_cell_selector_inventory(authorities, snapshot, bindings, *, classifier_record_set_store):
    """Read saved measurement columns and declared phenotype names without cell rows."""
    from .secondary_cell_inventory import phenotype_selector_inventory
    from .batch_measurement_artifacts import decode_committed_measurements_result
    from .batch_phenotyping.application_codec import decode_committed_cluster_label_import_result
    from .batch_phenotyping.semantic_records import normalize_committed_phenotype_application

    measured = [decode_committed_measurements_result(item) for item in authorities
                if item.artifact_ref.artifact_kind == "measurements_result"]
    sources = [item for item in authorities if item.artifact_ref.artifact_kind in {
        "marker_phenotype_result", "supervised_phenotype_result", "cluster_label_import_result"}]
    expected = set(bindings)
    for values in (measured, [item.artifact_ref.scope for item in sources]):
        covered = [binding for value in values for binding in value.analysis_domain_bindings]
        if set(covered) != expected or len(covered) != len(set(covered)):
            raise BatchArtifactError("Secondary review requires one completed measurement and phenotype result for each included tissue section.")
    inventories = []
    for source in sources:
        ref = source.artifact_ref
        matches = [item for item in measured
                   if set(item.analysis_domain_bindings) == set(ref.scope.analysis_domain_bindings)]
        if len(matches) != 1:
            raise BatchArtifactError("The completed measurements and phenotypes cover different tissue sections.")
        measurement = matches[0]
        if ref.artifact_kind == "cluster_label_import_result":
            identity = decode_committed_cluster_label_import_result(source).identity
            cell_ids, membership = identity["cell_ids_sha256"], identity["cell_to_domain_membership_sha256"]
        else:
            phenotype = normalize_committed_phenotype_application((source,))
            cell_ids, membership = phenotype.cell_ids_sha256, phenotype.cell_to_domain_membership_sha256
        if (cell_ids != measurement.cell_ids_sha256
                or membership != measurement.cell_to_domain_membership_sha256):
            raise BatchArtifactError("The completed phenotypes and measurements refer to different cells. Reopen the cell results before defining structures.")
        declared = phenotype_selector_inventory(
            ref, snapshot, classifier_record_set_store=classifier_record_set_store)
        inventories.append({
            **declared,
            "measurement_ids": list(measurement.selected_measurement_names),
            "channel_roles": [item["channel_id"] for item in measurement.canonical_channels],
            "measurement_channels": {item["measurement_name"]: item["channel_id"]
                for item in measurement.measurement_inventory
                if item["measurement_name"] in measurement.selected_measurement_names},
        })
    return _common_cell_selector_inventory(inventories)


def _definition_excluded_domain_sources(accepted_bindings, current, snapshot):
    """Account for image domains omitted from the late active-section review."""
    from .batch_classifier_inference_artifacts import decode_committed_primary_classifier_inference_result

    accepted, selected = set(accepted_bindings), set(current.analysis_domain_bindings)
    if not selected <= accepted:
        raise BatchArtifactError("Secondary definition regions differ from their accepted image domains")
    omitted = accepted - selected
    if not omitted:
        return ()
    nodes = {item.node_id: item for item in snapshot.plan.nodes}
    sources = []
    for binding in sorted(omitted, key=lambda item: (item.sample_id, item.project_entry_id, item.analysis_domain_id)):
        matches = [item for item in snapshot.committed_artifact_authorities
                   if item.artifact_ref.artifact_kind == "primary_inference_result"
                   and item.artifact_ref.scope.analysis_domain_bindings == (binding,)]
        if len(matches) != 1:
            raise BatchArtifactError("Every omitted secondary-review region requires its current primary exclusion decision")
        authority = matches[0]
        ref = authority.artifact_ref
        node = nodes.get(ref.producer_node_id)
        projection = decode_committed_primary_classifier_inference_result(authority)
        if (node is None or node.kind != "primary_classifier_inference"
                or node.analysis_domain_bindings != (binding,)
                or ref.scope.validation_domain_id != current.validation_domain_id
                or len(ref.configuration_bindings) != 1
                or not set(ref.configuration_bindings) <= set(current.configuration_bindings)
                or projection.domain_status not in {
                    "excluded_below_minimum", "excluded_by_primary_count_review"}):
            raise BatchArtifactError("An omitted secondary-review region has no current accepted primary exclusion")
        sources.append(ref)
    return tuple(sources)


def _definition_marker_review_scope(authorities):
    """Bind optional population gates to all current measured/phenotyped sections."""
    from .batch_measurement_artifacts import decode_committed_measurements_result

    kinds = {"measurements_result", "marker_phenotype_result", "supervised_phenotype_result",
             "cluster_label_import_result"}
    refs, sections, marker_names = set(), [], None
    for authority in authorities:
        reference = authority.artifact_ref
        if reference.artifact_kind in kinds:
            refs.add(reference.artifact_ref_sha256)
        if reference.artifact_kind == "measurements_result":
            measured = decode_committed_measurements_result(authority)
            sections.append((measured.slide_id, measured.section_id))
            available = {
                feature.measurement for feature in measured.measurement_plan.features
                if feature.kind == "expression" and feature.biomarker
                and feature.measurement in measured.selected_measurement_names
                and feature.measurement.rsplit(": ", 1)[-1] in {"Mean", "Median"}
            }
            marker_names = available if marker_names is None else marker_names & available
    if len(set(sections)) != len(sections):
        raise BatchArtifactError(
            "Secondary marker review needs distinct image and section identifiers across samples. "
            "Choose unique slide identifiers before combining these images.")
    return {"source_ref_sha256s": sorted(refs), "marker_measurement_ids": sorted(marker_names or ()),
            "sections": [{"slide_id": slide, "section_id": section} for slide, section in sorted(sections)]}


def definition_review_context(context, *, dependencies):
    """Read the actual primary settings and channels of the current group inputs."""
    from .batch_classifier_inference_artifacts import decode_committed_primary_classifier_inference_result
    from .batch_training.planning import primary_plan_review_application_inventory
    from .batch_training.runtime import decode_current_committed_training_plan_set

    store = dependencies.state_store
    current = store.authenticate_current_execution_context(context)
    snapshot = store.reopen_execution_snapshot(expected_head_sha256=current.state_head_sha256)
    if current.node_kind != NODE_KIND or current.node_scope != "validation_domain":
        raise BatchArtifactError("Secondary definitions require their current group review")
    samples = {item.sample_id: item for item in snapshot.manifest.enabled_samples}
    selected = tuple(samples[key] for key in current.participant_ids)
    if (any(item.config.secondary_structures is not None for item in selected)
            or {item.validation_domain_id for item in selected} != {current.validation_domain_id}):
        raise BatchArtifactError("The secondary definition choice is already configured or belongs to another group")
    authorities = tuple(store.authenticate_available_artifact(ref) for ref in context.inputs)
    entries, bindings, _sources = primary_plan_review_application_inventory(
        manifest=snapshot.manifest,
        input_authorities=tuple(item for item in authorities if item.artifact_ref.artifact_kind in {
            "project_catalogue_entry", "analysis_frame", "accepted_analysis_domain",
        }),
    )
    if (tuple(sorted(item.sample_id for item in entries)) != current.participant_ids
            or tuple(sorted(item.project_entry_id for item in entries)) != current.project_entry_ids):
        raise BatchArtifactError("Secondary definition image or region coverage changed")
    excluded_sources = _definition_excluded_domain_sources(bindings, current, snapshot)
    panels = tuple(tuple(item.to_mapping() for item in entry.channels) for entry in entries)
    if any(panel != panels[0] for panel in panels[1:]):
        raise BatchArtifactError("Secondary structures require matching channel panels within this processing group")
    nodes = {item.node_id: item for item in snapshot.plan.nodes}
    primary = tuple(decode_committed_primary_classifier_inference_result(item)
                    for item in authorities if item.artifact_ref.artifact_kind == "primary_inference_result")
    skipped_plans = tuple(decode_current_committed_training_plan_set(
        item, store=dependencies.training_plan_set_store, state_store=store,
    ) for item in authorities if item.artifact_ref.artifact_kind == "training_plan_set")
    if any(item.plan_set.skipped_primary is None for item in skipped_plans):
        raise BatchArtifactError("Secondary definition setup inputs must be explicit primary skips")
    primary_plan_ids = set()
    for item in primary:
        producer = nodes[item.artifact_ref.producer_node_id]
        matches = {key for key in producer.dependency_ids
                   if nodes[key].kind == "primary_classifier_training_plan_review"}
        if len(matches) != 1:
            raise BatchArtifactError("Primary classification lacks its exact accepted settings")
        primary_plan_ids.update(matches)
    plans = tuple(decode_current_committed_training_plan_set(
        item, store=dependencies.training_plan_set_store, state_store=store,
    ) for item in snapshot.committed_artifact_authorities
        if item.producer_node_kind == "primary_classifier_training_plan_review"
        and item.artifact_ref.producer_node_id in primary_plan_ids
        and item.artifact_ref.artifact_kind == "training_plan_set")
    if {item.artifact_ref.producer_node_id for item in plans} != primary_plan_ids:
        raise BatchArtifactError("Refresh the accepted primary classifier settings")
    plans = (*plans, *skipped_plans)
    settings, parent_ids = _definition_primary_settings(
        primary=primary, plans=plans, bindings=current.analysis_domain_bindings)
    validation_config = selected[0].config.to_mapping()
    validation_config.update(settings[0].to_mapping() if settings else {
        "primary_classes": None, "primary_classifier": None})
    validation_config["channels"] = list(panels[0])
    # Thresholds can have been accepted independently of the current phenotype
    # route. Only current gates in this group and its exact tissue domains may
    # be considered; the threshold reader also matches measurement semantics.
    marker_gate_refs = {
        item.artifact_ref.artifact_ref_sha256
        for item in snapshot.committed_artifact_authorities
        if item.artifact_ref.artifact_kind == 'marker_gate'
        and item.artifact_ref.scope.validation_domain_id == current.validation_domain_id
        and item.artifact_ref.scope.analysis_domain_bindings
        and set(item.artifact_ref.scope.analysis_domain_bindings) <= set(current.analysis_domain_bindings)
    }
    value = {
        "schema_version": CONTEXT_SCHEMA,
        "batch_id": snapshot.head.batch_id,
        "validation_domain_id": current.validation_domain_id,
        "participant_ids": list(current.participant_ids),
        "configuration_bindings": [item.to_mapping() for item in current.configuration_bindings],
        "analysis_domain_bindings": [item.to_mapping() for item in current.analysis_domain_bindings],
        "project_entry_ids": list(current.project_entry_ids),
        "validation_config": validation_config,
        "profile_directory": str(store.layout.state / "secondary-profiles"),
        "eligible_primary_class_ids": sorted(parent_ids),
        "cell_selector_inventory": _definition_cell_selector_inventory(
            authorities, snapshot, current.analysis_domain_bindings,
            classifier_record_set_store=dependencies.classifier_record_set_store),
        "marker_review_scope": _definition_marker_review_scope(authorities),
        "source_ref_sha256s": sorted({item.artifact_ref_sha256 for item in context.inputs}
                                    | {item.artifact_ref.artifact_ref_sha256 for item in plans}
                                    | {item.artifact_ref_sha256 for item in excluded_sources}
                                    | marker_gate_refs),
    }
    initial = secondary_definition_initial_values(nodes[current.node_id], store)
    if initial is not None:
        value["initial_secondary_structures"] = initial
    return validate_definition_context(value)


def secondary_definition_review(value):
    from mif_pipeline.pipeline_core import InteractionReason, StageResult

    value = validate_definition_context(value)
    return StageResult.needs_interaction(
        stage=NODE_KIND, slide_id=value["validation_config"]["slide_id"],
        reason=InteractionReason.OTHER_EXPLICIT_REASON,
        message="Choose the structures to identify, where to look for them and the criteria they must meet.",
        allowed_actions=("accept",), resume_context=value,
        checkpoint_id="secondary-definitions-" + _digest(value),
    )


@dataclass(frozen=True, slots=True)
class SecondaryDefinitionRuntimeAdapter:
    spec: object
    dependencies: object

    def execute_committed(self, context, *, workspace_lease, terminal_committer):
        from .batch_artifacts import BatchNodeExecutionResult
        from .batch_state import BatchRawNodeOutcome
        from .batch_training import validate_authorized_training_interaction_response
        from mif_pipeline.pipeline_core import INTERACTION_ACTION_SCHEMA_VERSION, StageResult

        del workspace_lease
        if context.adapter_spec != self.spec:
            raise BatchArtifactError("Secondary definition review received a foreign adapter")
        store = self.dependencies.state_store
        value = definition_review_context(context, dependencies=self.dependencies)
        request = secondary_definition_review(value)
        authorized = store.load_authorized_interaction_response(context)
        outputs = ()
        result = request
        if authorized is not None:
            checked = validate_authorized_training_interaction_response(authorized, expected_context=context)
            previous = store.load_attempt(checked.accepted_response["prior_terminal_record_sha256"])
            if (checked.action != "accept" or previous.stage_result != request
                    or checked.accepted_response["response_validator_id"] != RESPONSE_VALIDATOR_ID
                    or checked.accepted_response["response_validator_version"] != INTERACTION_ACTION_SCHEMA_VERSION):
                raise BatchArtifactError("The secondary definition review changed; reopen its current choice")
            structures, declarations = _accepted_choice_material(
                checked.response, value, materialize=True,
            )
            identity = {
                "selection_context": value,
                "secondary_structures": [item.to_mapping() for item in structures],
                "profile_declarations": [item.to_mapping() for item in declarations],
                "source_ref_sha256s": value["source_ref_sha256s"],
                "interaction_response_sha256": checked.accepted_response["response_sha256"],
                "interaction_context_sha256": _digest(value),
            }
            selection_sha256 = _digest(identity)
            identity["selection_sha256"] = selection_sha256
            payload = canonical_json_bytes({"schema_name": SCHEMA_NAME, "schema_version": 1,
                                            "selection": _plain(identity)})
            body_sha256 = hashlib.sha256(payload).hexdigest()
            path = store.layout.state / "secondary-definitions" / f"{body_sha256}.json"
            publish_immutable_record(path, payload, max_bytes=_MAX_SELECTION_BYTES,
                                     label="secondary definition selection")
            from .batch_artifacts import AnalysisDomainBinding

            contract = secondary_definition_identity_contract()
            outputs = (BatchArtifactRef(
                artifact_id="secondary-definitions-" + selection_sha256[:48], artifact_kind=ARTIFACT_KIND,
                schema_name=SCHEMA_NAME, schema_version=1, origin=ArtifactOrigin.INTERNAL_NODE,
                producer_node_id=context.node_id, producer_attempt_id=context.authorization.attempt_id,
                producer_attempt_authorization_sha256=context.authorization.authorization_sha256,
                external_declaration_sha256=None, external_validation_receipt_sha256=None,
                locator=None, sha256=None, byte_count=None,
                scope=ArtifactScope(value["validation_domain_id"], None, None, None,
                    tuple(AnalysisDomainBinding.from_mapping(item) for item in value["analysis_domain_bindings"])),
                owning_identity={**{name: identity[name] for name in _IDENTITY_FIELDS[:-2]},
                                 "body_sha256": body_sha256, "body_byte_count": len(payload)}, identity_contract=contract,
                owning_identity_contract_sha256=contract.contract_sha256,
                producer_version=PRODUCER_VERSION,
                runtime_versions={"mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION},
                compatibility_signature_sha256=context.compatibility_signature_sha256,
                configuration_bindings=tuple(ArtifactConfigurationBinding.from_mapping(item)
                                             for item in value["configuration_bindings"]),
            ),)
            result = StageResult.completed(stage=NODE_KIND, slide_id=request.slide_id, generation_id=selection_sha256)
        return terminal_committer.commit_raw((BatchRawNodeOutcome(
            context=context, stage_result=result,
            execution_result=BatchNodeExecutionResult(context.authorization.authorization_sha256, outputs),
            execution_exchange_sha256=None,
        ),))[0]


def build_secondary_definition_interaction_owner(*, dependencies):
    from .batch_interactions import BatchInteractionOwnerBinding, BatchTargetResponseValidation
    from mif_pipeline.pipeline_core import INTERACTION_ACTION_SCHEMA_VERSION, InteractionReason

    def validate(target, action, response):
        result = target.stage_result
        if (result.stage != NODE_KIND or result.interaction_reason is not InteractionReason.OTHER_EXPLICIT_REASON
                or action != "accept" or result != secondary_definition_review(result.resume_context)):
            raise BatchArtifactError("Refresh the secondary structure definition review")
        validate_secondary_definition_choice(response, result.resume_context)
        store = dependencies.state_store
        head = store.load_head()
        request = store.reopen_interaction_base_authorization_request(
            target.interaction_request_id, expected_head_sha256=head.sha256,
        )
        return BatchTargetResponseValidation(
            target_id=target.target_id, node_id=target.node_id, status="accepted",
            response=_plain(response),
            authorization_request=request, response_validator_id=RESPONSE_VALIDATOR_ID,
            response_validator_version=INTERACTION_ACTION_SCHEMA_VERSION, rejection_code=None,
        )
    return BatchInteractionOwnerBinding(
        stage=NODE_KIND, interaction_reason=InteractionReason.OTHER_EXPLICIT_REASON.value,
        action_schema_version=INTERACTION_ACTION_SCHEMA_VERSION, validate_target=validate,
    )
