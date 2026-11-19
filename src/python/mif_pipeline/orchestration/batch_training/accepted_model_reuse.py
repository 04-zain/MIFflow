"""Export accepted native pixel models and admit their unchanged bytes for reuse.

We authenticate training through its available artifact owner before exporting.
An admitted external set then belongs to the target's retained state. Training
provenance stays unchanged while the ordinary application owner binds its new
image, channels and domains. Export packages must be mounted at the same path
on the native worker; this owner does not invent a remote locator mapping.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from typing import Mapping

from mif_pipeline.io.pinned_files import open_pinned_member
from mif_pipeline.pipeline_core.config import ClassifierMode, PrimaryClassifierSettings
from mif_pipeline.pipeline_core.durable_records import DurableDirectoryAuthority

from ..batch_artifacts import (
    ArtifactOrigin, ArtifactProjectionTarget, ArtifactScope, BatchArtifactRef,
    BatchCommittedArtifactAuthority, external_artifact_ref,
)
from ..batch_manifest import (
    ReusableArtifactDeclaration, ValidatedBatchManifest, issue_validated_reusable_artifact,
)
from ..batch_state import BatchStateStore
from ._shared import BatchTrainingError, ClassifierArtifactKind, ClassifierKind
from .record_publication import (
    ClassifierRecordSetStore, decode_current_committed_classifier_record_plan_set,
)
from .records import ClassifierRecord, ClassifierRecordSet, PixelClassifierOwnerPayload
from .runtime import TrainingPlanSetStore
from .stage_adapters import classifier_record_set_identity_contract

_EXPORT_SCHEMA = "mif-accepted-native-classifier-export"
_OWNER_VERSION = "accepted-native-classifier-reuse/v1"
_MAX_HEADER_BYTES = 16 * 1024 * 1024
_MAX_MODEL_BYTES = 64 * 1024 * 1024
_HEADER_FIELDS = frozenset({
    "schema_name", "schema_version", "source_head_sha256", "source_artifact_ref_sha256",
    "source_terminal_record_sha256", "record_set", "primary_settings", "owning_identity",
    "runtime_versions", "compatibility_signature_sha256",
})


def _bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _model_member(record: ClassifierRecord) -> str:
    return f"models/{record.native_artifact_sha256}.json"


def _model_bytes(root: Path, record: ClassifierRecord) -> bytes:
    with open_pinned_member(root, _model_member(record)) as opened:
        if opened.size != record.native_artifact_byte_count or opened.size > _MAX_MODEL_BYTES:
            raise BatchTrainingError("Saved classifier model size differs from its accepted record")
        data = opened.stream.read(_MAX_MODEL_BYTES + 1)
        if hashlib.sha256(data).hexdigest() != record.native_artifact_sha256:
            raise BatchTrainingError("Saved classifier model differs from its accepted record")
        return data


def _require_native_pixel_set(record_set: ClassifierRecordSet) -> None:
    if record_set.classifier_kind not in {
        ClassifierKind.PRIMARY_COMPARTMENT, ClassifierKind.SECONDARY_STRUCTURE,
    } or any(
        item.review_decision != "accepted"
        or item.artifact_kind is not ClassifierArtifactKind.QUPATH_NATIVE
        or type(item.owner_payload) is not PixelClassifierOwnerPayload
        for item in record_set.classifier_records
    ):
        raise BatchTrainingError("Reuse requires an accepted native pixel classifier set")


@dataclass(frozen=True, slots=True)
class ImportedNativeClassifierSet:
    """Strict export material reopened under an admitted target artifact ref."""

    path: Path
    header: Mapping[str, object]
    record_set: ClassifierRecordSet
    primary_settings: PrimaryClassifierSettings | None
    sha256: str
    byte_count: int
    deployment_settings: PrimaryClassifierSettings | None = None

    def model_path(self, record: ClassifierRecord) -> Path:
        if record not in self.record_set.classifier_records:
            raise BatchTrainingError("Saved classifier is absent from its admitted set")
        # Recheck the same exported member before projecting its native locator.
        _model_bytes(self.path.parent, record)
        return self.path.parent / _model_member(record)


def read_native_classifier_export(path: Path) -> ImportedNativeClassifierSet:
    path = path.absolute()
    if path.name != "export.json":
        raise BatchTrainingError("Saved classifier selection must name export.json")
    with open_pinned_member(path.parent, path.name) as opened:
        if not 0 < opened.size <= _MAX_HEADER_BYTES:
            raise BatchTrainingError("Saved classifier export exceeds its header size bound")
        payload = opened.stream.read(_MAX_HEADER_BYTES + 1)
        try:
            header = json.loads(payload, parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"Nonfinite export value: {value}")))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BatchTrainingError("Saved classifier export is not finite UTF-8 JSON") from exc
        if (not isinstance(header, dict) or set(header) != _HEADER_FIELDS
                or header["schema_name"] != _EXPORT_SCHEMA or header["schema_version"] != 1
                or payload != _bytes(header)):
            raise BatchTrainingError("Saved classifier export is not a canonical closed header")
        for name in ("source_head_sha256", "source_artifact_ref_sha256",
                     "source_terminal_record_sha256", "compatibility_signature_sha256"):
            value = header[name]
            if not isinstance(value, str) or len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise BatchTrainingError("Saved classifier source identity is invalid")
        record_set = ClassifierRecordSet.from_mapping(header["record_set"])
        _require_native_pixel_set(record_set)
        settings = (None if header["primary_settings"] is None
                    else PrimaryClassifierSettings.from_mapping(header["primary_settings"]))
        if (record_set.classifier_kind is ClassifierKind.PRIMARY_COMPARTMENT) != (settings is not None):
            raise BatchTrainingError("Saved classifier export settings differ from its role")
        if settings is not None:
            if len(record_set.classifier_records) != 1:
                raise BatchTrainingError("Saved primary settings require one accepted classifier")
            record = record_set.classifier_records[0]
            definition = record.owner_payload.classifier_definition
            selected = settings.primary_classifier
            if (selected.classifier_id != record.classifier_id
                    or selected.validation_domain_id != record.validation_domain_id
                    or selected.channel_roles != definition.channel_roles
                    or selected.output_classes != definition.output_classes):
                raise BatchTrainingError("Saved primary classes or channel inputs differ from the accepted model")
        contract = classifier_record_set_identity_contract()
        contract.validate(artifact_kind="classifier_record_set", schema_name=contract.schema_name,
                          schema_version=contract.schema_version,
                          owning_identity=header["owning_identity"],
                          runtime_versions=header["runtime_versions"])
        identity = header["owning_identity"]
        if (identity["classifier_record_set_sha256"] != record_set.classifier_record_set_sha256
                or identity["source_training_plan_set_sha256"] != record_set.source_training_plan_set_sha256
                or identity["classifier_kind"] != record_set.classifier_kind.value
                or identity["validation_domain_id"] != record_set.validation_domain_id
                or identity["output_inventory"] != [list(item) for item in record_set.output_inventory]):
            raise BatchTrainingError("Saved classifier identity differs from its immutable records")
        for record in record_set.classifier_records:
            _model_bytes(path.parent, record)
        return ImportedNativeClassifierSet(path, header, record_set, settings,
                                            hashlib.sha256(payload).hexdigest(), len(payload))


@dataclass(frozen=True, slots=True)
class AcceptedNativeClassifierExport:
    """A portable accepted model package with its recorded source provenance."""

    material: ImportedNativeClassifierSet

    def declaration(self) -> ReusableArtifactDeclaration:
        """Describe these verified files for the destination analysis."""
        material = self.material
        contract = classifier_record_set_identity_contract()
        return ReusableArtifactDeclaration(
            artifact_id=f"saved-classifier-{material.record_set.classifier_record_set_sha256}",
            artifact_kind="classifier_record_set",
            schema_name=contract.schema_name, schema_version=str(contract.schema_version),
            locator=material.path, sha256=material.sha256, byte_count=material.byte_count,
            owning_identity=material.header["owning_identity"],
            runtime_versions=material.header["runtime_versions"],
            validation_domain_id=material.record_set.validation_domain_id,
            compatibility_signature_sha256=material.header["compatibility_signature_sha256"],
        )


def export_accepted_native_classifier_set(
    *, state_store: BatchStateStore, record_store: ClassifierRecordSetStore,
    plan_store: TrainingPlanSetStore, expected_head_sha256: str,
    record_ref_sha256: str, destination: Path,
) -> AcceptedNativeClassifierExport:
    snapshot = state_store.reopen_execution_snapshot(expected_head_sha256=expected_head_sha256)
    matches = tuple(item for item in snapshot.accepted_artifacts
                    if item.artifact_kind == "classifier_record_set"
                    and item.artifact_ref_sha256 == record_ref_sha256)
    if len(matches) != 1:
        raise BatchTrainingError("Saved classifier selection is not available at this analysis head")
    source = matches[0]
    authority = state_store.authenticate_available_artifact(source)
    record_set, projection = decode_current_committed_classifier_record_plan_set(
        authority, record_store=record_store, plan_store=plan_store, state_store=state_store,
    )
    _require_native_pixel_set(record_set)
    settings = (projection.plan_set.plans[0].request.primary_settings
                if record_set.classifier_kind is ClassifierKind.PRIMARY_COMPARTMENT else None)
    header = {
        "schema_name": _EXPORT_SCHEMA, "schema_version": 1,
        "source_head_sha256": snapshot.head.sha256,
        "source_artifact_ref_sha256": source.artifact_ref_sha256,
        "source_terminal_record_sha256": authority.terminal_record_sha256,
        "record_set": record_set.to_mapping(),
        "primary_settings": None if settings is None else settings.to_mapping(),
        "owning_identity": _plain(source.owning_identity),
        "runtime_versions": dict(source.runtime_versions),
        "compatibility_signature_sha256": source.compatibility_signature_sha256,
    }
    destination = destination.absolute()
    # Reserve an absent package. Immutable publication owns its retained parents.
    destination.mkdir(exist_ok=False)
    with DurableDirectoryAuthority(destination, create=False) as package:
        with package.retain_child(Path("models"), create=True) as models:
            for record in record_set.classifier_records:
                if record.native_source_project_identity_sha256 != snapshot.head.project_identity_sha256:
                    raise BatchTrainingError("Accepted native model belongs to a different source project")
                with open_pinned_member(Path(snapshot.head.project_path).parent,
                                        record.native_artifact_project_relative_path) as opened:
                    if opened.size != record.native_artifact_byte_count or opened.size > _MAX_MODEL_BYTES:
                        raise BatchTrainingError("Accepted native model size differs")
                    payload = opened.stream.read(_MAX_MODEL_BYTES + 1)
                    if hashlib.sha256(payload).hexdigest() != record.native_artifact_sha256:
                        raise BatchTrainingError("Accepted native model bytes differ")
                    models.publish_bytes(f"{record.native_artifact_sha256}.json", payload,
                                             max_bytes=_MAX_MODEL_BYTES, label="accepted native classifier")
        if state_store.authenticate_available_artifact(source) != authority:
            raise BatchTrainingError("Accepted classifier source changed during export")
        package.publish_immutable("export.json", _bytes(header), max_bytes=_MAX_HEADER_BYTES,
                                  label="accepted classifier export")
    return AcceptedNativeClassifierExport(
        read_native_classifier_export(destination / "export.json")
    )


def _receipt(material: ImportedNativeClassifierSet, declaration: ReusableArtifactDeclaration,
             target: ArtifactProjectionTarget) -> str:
    return _digest({"owner_version": _OWNER_VERSION, "export_sha256": material.sha256,
                    "source_artifact_ref_sha256": material.header["source_artifact_ref_sha256"],
                    "source_terminal_record_sha256": material.header["source_terminal_record_sha256"],
                    "declaration_sha256": declaration.declaration_sha256,
                    "target": target.to_mapping()})


def validate_export_declaration(material: ImportedNativeClassifierSet,
                                declaration: ReusableArtifactDeclaration) -> None:
    contract = classifier_record_set_identity_contract()
    if (declaration.artifact_kind != "classifier_record_set"
            or (declaration.schema_name, declaration.schema_version) != (contract.schema_name, str(contract.schema_version))
            or declaration.locator.absolute() != material.path
            or declaration.sha256 != material.sha256 or declaration.byte_count != material.byte_count
            or _plain(declaration.owning_identity) != material.header["owning_identity"]
            or dict(declaration.runtime_versions) != material.header["runtime_versions"]
            or declaration.validation_domain_id != material.record_set.validation_domain_id
            or declaration.compatibility_signature_sha256 != material.header["compatibility_signature_sha256"]):
        raise BatchTrainingError("Saved classifier declaration differs from its authenticated export")


def _selected_classifier_samples(manifest, material, declaration):
    """Resolve this declaration's exact destinations and check their model settings."""
    selected_samples = []
    for sample in manifest.enabled_samples:
        if sample.validation_domain_id != material.record_set.validation_domain_id:
            continue
        primary = sample.config.primary_classifier
        if material.record_set.classifier_kind is ClassifierKind.PRIMARY_COMPARTMENT:
            selected = (sample.sample_id in declaration.target_sample_ids if declaration.target_sample_ids else
                        primary is not None and primary.mode is ClassifierMode.SAVED and
                        primary.artifact_path.absolute() == material.path)
            if selected:
                validate_imported_primary_settings(sample.config, material)
        else:
            structures = sample.config.secondary_structures
            selected = bool(structures) and all(
                item.classifier.mode is ClassifierMode.SAVED
                and item.classifier.artifact_path.absolute() == material.path for item in structures)
            if selected and set(dict(material.record_set.output_inventory)) != {
                    item.classifier.classifier_id
                    for item in structures}:
                raise BatchTrainingError("Saved secondary set lacks complete configured output coverage")
            if selected:
                inventory = dict(material.record_set.output_inventory)
                records = {item.classifier_record_sha256: item for item in material.record_set.classifier_records}
                for structure in structures:
                    output_id = structure.classifier.classifier_id
                    record = records[inventory[output_id]]
                    classifier = structure.classifier
                    if (classifier.classifier_id != record.classifier_id
                            or classifier.classifier_version != record.classifier_version
                            or classifier.classifier_sha256 not in (None, record.native_artifact_sha256)):
                        raise BatchTrainingError("Saved secondary selection differs from its accepted model")
        if selected:
            selected_samples.append(sample)
    return tuple(selected_samples)


def import_batch_classifier_record_sets(
    manifest: ValidatedBatchManifest, *,
    retained_artifacts: tuple[BatchArtifactRef, ...] = (),
) -> tuple[BatchArtifactRef, ...]:
    declarations = tuple(item for item in manifest.manifest.reusable_artifacts
                         if item.artifact_kind == "classifier_record_set")
    contract = classifier_record_set_identity_contract()
    refs = []
    retained = {(ref.external_declaration_sha256, ref.scope, ref.configuration_bindings): ref
                for ref in retained_artifacts
                if ref.origin is ArtifactOrigin.MANIFEST_EXTERNAL
                and ref.artifact_kind == "classifier_record_set"}
    used = set()
    enabled = {item.sample_id: item for item in manifest.enabled_samples}
    for declaration in declarations:
        if any(sid not in enabled or enabled[sid].validation_domain_id != declaration.validation_domain_id
               for sid in declaration.target_sample_ids):
            raise BatchTrainingError("Saved classifier targets differ from this analysis's enabled images")
        material = read_native_classifier_export(declaration.locator)
        validate_export_declaration(material, declaration)
        material = _with_deployment_settings(material, declaration)
        for sample in _selected_classifier_samples(manifest, material, declaration):
            target = ArtifactProjectionTarget.from_manifest(manifest, (sample.sample_id,), ArtifactScope(
                sample.validation_domain_id, sample.sample_id, sample.slide_id, None, ()))
            previous = retained.get((declaration.declaration_sha256, target.scope, target.configuration_bindings))
            receipt = (_receipt(material, declaration, target) if previous is None
                       else previous.external_validation_receipt_sha256)
            accepted = issue_validated_reusable_artifact(
                manifest=manifest, declaration=declaration,
                owning_validation_receipt_sha256=receipt,
                producer_version=_OWNER_VERSION, owning_identity_contract_sha256=contract.contract_sha256,
            )
            current = external_artifact_ref(accepted, target, manifest=manifest, identity_contract=contract)
            if previous is not None and current != previous:
                raise BatchTrainingError("Retained saved classifier differs from its current declaration or destination")
            refs.append(current)
            used.add(declaration.artifact_id)
    if used != {item.artifact_id for item in declarations}:
        raise BatchTrainingError("Saved classifier declaration has no configured deployment target")
    for sample in manifest.enabled_samples:
        roles = {item.owning_identity["classifier_kind"] for item in refs
                 if any(binding.sample_id == sample.sample_id for binding in item.configuration_bindings)}
        if (sample.config.primary_classifier is not None
                and sample.config.primary_classifier.mode is ClassifierMode.SAVED
                and "primary_compartment" not in roles):
            raise BatchTrainingError("Saved primary classifier requires an accepted record-set export")
        if (sample.config.secondary_structures is not None
                and any(item.classifier.mode is ClassifierMode.SAVED for item in sample.config.secondary_structures)
                and "secondary_structure" not in roles):
            raise BatchTrainingError("Saved secondary classifiers require one complete accepted set export")
    return tuple(refs)


def validate_imported_primary_settings(config: object, material: ImportedNativeClassifierSet) -> PrimaryClassifierSettings:
    if material.primary_settings is None:
        raise BatchTrainingError("Saved primary classifier requires its accepted settings")
    selected = material.deployment_settings
    if selected is None:
        if config.primary_classifier is None or config.primary_classes is None:
            raise BatchTrainingError("Saved primary classifier requires concrete target settings")
        selected = PrimaryClassifierSettings(config.primary_classes, config.primary_classifier)
    source = material.primary_settings
    original = source.primary_classifier
    deployment = selected.primary_classifier
    if (deployment.mode is not ClassifierMode.SAVED
            or deployment.artifact_path.absolute() != material.path
            or deployment.classifier_version != material.record_set.classifier_records[0].classifier_version
            or selected.primary_classes != source.primary_classes
            or deployment.classifier_id != original.classifier_id
            or deployment.classifier_sha256 not in (None, material.record_set.classifier_records[0].native_artifact_sha256)
            or deployment.channel_roles != original.channel_roles
            or deployment.output_classes != original.output_classes
            or deployment.require_ignore_training != original.require_ignore_training
            or deployment.validation_domain_id != original.validation_domain_id):
        raise BatchTrainingError("Saved primary classifier target differs from accepted class/feature settings")
    return selected


def decode_imported_classifier_record_set(
    authority: BatchCommittedArtifactAuthority, *, state_store: BatchStateStore,
) -> ImportedNativeClassifierSet:
    ref = authority.artifact_ref
    if (ref.origin is not ArtifactOrigin.MANIFEST_EXTERNAL or ref.artifact_kind != "classifier_record_set"
            or ref.producer_version != _OWNER_VERSION
            or state_store.authenticate_available_manifest_external_artifact(ref) != authority):
        raise BatchTrainingError("Saved classifier lacks current admitted external authority")
    snapshot = state_store.reopen_execution_snapshot(expected_head_sha256=authority.state_head_sha256)
    return validate_imported_classifier_ref(snapshot.manifest, ref)


def validate_imported_classifier_ref(
    manifest: ValidatedBatchManifest, ref: BatchArtifactRef,
) -> ImportedNativeClassifierSet:
    """Reopen exact admitted material; current state authentication is the caller's duty."""
    if ref.origin is not ArtifactOrigin.MANIFEST_EXTERNAL or ref.producer_version != _OWNER_VERSION:
        raise BatchTrainingError("Saved classifier ref is not an accepted-model import")
    declarations = tuple(item for item in manifest.manifest.reusable_artifacts
                         if item.declaration_sha256 == ref.external_declaration_sha256)
    if len(declarations) != 1:
        raise BatchTrainingError("Saved classifier declaration is absent from the current manifest")
    material = read_native_classifier_export(ref.locator)
    validate_export_declaration(material, declarations[0])
    declaration = declarations[0]
    material = _with_deployment_settings(material, declaration)
    selected = {sample.sample_id for sample in _selected_classifier_samples(manifest, material, declaration)}
    participants = tuple(item.sample_id for item in ref.configuration_bindings)
    if len(participants) != 1 or not set(participants) <= selected:
        raise BatchTrainingError("Saved classifier is not selected for this image")
    sample = next(sample for sample in manifest.enabled_samples if sample.sample_id == participants[0])
    target = ArtifactProjectionTarget.from_manifest(manifest, participants, ArtifactScope(
        sample.validation_domain_id, sample.sample_id, sample.slide_id, None, ()))
    contract = classifier_record_set_identity_contract()
    # The retained state authenticates the original admission receipt. Rebuild
    # its exact destination and content here; another image's later selection
    # does not change this model input.
    accepted = issue_validated_reusable_artifact(manifest=manifest, declaration=declaration,
        owning_validation_receipt_sha256=ref.external_validation_receipt_sha256,
        producer_version=_OWNER_VERSION, owning_identity_contract_sha256=contract.contract_sha256)
    if external_artifact_ref(accepted, target, manifest=manifest, identity_contract=contract) != ref:
        raise BatchTrainingError("Saved classifier differs from its current declaration or destination")
    return material


def _with_deployment_settings(material, declaration):
    """Read the current image-side choices while retaining the original fitted record."""
    if declaration.primary_settings is None:
        return material
    selected = PrimaryClassifierSettings.from_mapping(declaration.primary_settings)
    result = replace(material, deployment_settings=selected)
    validate_imported_primary_settings(None, result)
    return result


def retain_selected_primary_export(*, path: Path, expected_sha256: str, workspace_root: Path,
                                   application_entries, analysis_domain_bindings,
                                   batch_id: str, input_refs):
    """Copy one reviewed accepted package into this analysis and bind its destination images."""
    from .planning import SavedClassifierSelection
    material = read_native_classifier_export(path)
    if material.sha256 != expected_sha256 or material.primary_settings is None:
        raise BatchTrainingError("The selected saved model changed. Choose and review it again.")
    for entry in application_entries:
        material.primary_settings.validate_channels(entry.channels)
    domains = {item.scope.validation_domain_id for item in input_refs}
    if domains != {material.record_set.validation_domain_id}:
        raise BatchTrainingError("The saved classifier belongs to a different validation group")
    destination = workspace_root / "models" / "saved" / material.sha256
    destination.mkdir(parents=True, exist_ok=True)
    with DurableDirectoryAuthority(destination, create=False) as package:
        with package.retain_child(Path("models"), create=True) as models:
            for record in material.record_set.classifier_records:
                models.publish_bytes(f"{record.native_artifact_sha256}.json",
                    _model_bytes(material.path.parent, record), max_bytes=_MAX_MODEL_BYTES,
                    label="saved native classifier")
        package.publish_immutable("export.json", _bytes(material.header), max_bytes=_MAX_HEADER_BYTES,
                                  label="saved classifier export")
    material = read_native_classifier_export(destination / "export.json")
    settings = material.primary_settings.to_mapping()
    record = material.record_set.classifier_records[0]
    settings["primary_classifier"].update(mode="saved", artifact_path=str(material.path),
        classifier_version=record.classifier_version, classifier_sha256=record.native_artifact_sha256)
    targets = tuple(sorted(item.sample_id for item in application_entries))
    declaration = replace(AcceptedNativeClassifierExport(material).declaration(),
        artifact_id="saved-classifier-" + _digest({
            "classifier_record_set_sha256": material.record_set.classifier_record_set_sha256,
            "target_sample_ids": list(targets)}),
        target_sample_ids=targets, primary_settings=settings)
    _with_deployment_settings(material, declaration)
    return SavedClassifierSelection(batch_id, declaration,
        tuple(sorted(application_entries, key=lambda item: item.project_entry_id)),
        analysis_domain_bindings, tuple(sorted(item.artifact_ref_sha256 for item in input_refs)))


def manifest_for_classifier_setup(manifest, projection):
    """Change only this accepted setup's saved-model declaration.

    The image sources, image settings and unrelated model selections retain
    their exact bytes. The setup record supplies the destination image IDs.
    """
    return _manifest_for_classifier_setup_material(manifest, projection.plan_set, projection.participant_ids)


def _manifest_for_classifier_setup_material(manifest, plan_set, participant_ids):
    """Derive the declaration from an authenticated current or historical setup."""
    from ..batch_manifest import revalidate_retained_manifest
    if plan_set.classifier_kind is not ClassifierKind.PRIMARY_COMPARTMENT:
        raise BatchTrainingError("Saved selection requires primary tissue classification")
    selected = plan_set.saved_selection
    targets = set(participant_ids)
    declarations = []
    for declaration in manifest.manifest.reusable_artifacts:
        configured_targets = declaration.target_sample_ids or tuple(
            sample.sample_id for sample in manifest.enabled_samples
            if sample.config.primary_classifier is not None
            and sample.config.primary_classifier.mode is ClassifierMode.SAVED
            and sample.config.primary_classifier.artifact_path.absolute() == declaration.locator.absolute())
        if (declaration.artifact_kind == "classifier_record_set"
                and declaration.owning_identity.get("classifier_kind") == "primary_compartment"
                and targets.intersection(configured_targets)):
            if not set(configured_targets) <= targets:
                raise BatchTrainingError("Review every image sharing this saved classifier before replacing it")
            continue
        declarations.append(declaration)
    if selected is not None:
        declarations.append(selected.declaration)
    value = manifest.manifest.to_mapping()
    if declarations:
        value["reusable_artifacts"] = [item.to_mapping() for item in sorted(declarations, key=lambda item: item.artifact_id)]
    else:
        value.pop("reusable_artifacts", None)
    payload = _bytes(value)
    return revalidate_retained_manifest(payload, payload)


def validate_classifier_manifest_transition(previous, current):
    """Identify a declaration-only change before the state owner checks its accepted setup."""
    before, after = previous.manifest.to_mapping(), current.manifest.to_mapping()
    old, new = before.pop("reusable_artifacts", []), after.pop("reusable_artifacts", [])
    if before != after or old == new:
        raise BatchTrainingError("Saved model selection must retain every image and analysis setting")
    changed = [item for item in old if item not in new] + [item for item in new if item not in old]
    if not changed or any(item.get("artifact_kind") != "classifier_record_set"
            or item.get("owning_identity", {}).get("classifier_kind") != "primary_compartment"
            for item in changed):
        raise BatchTrainingError("Only an explicitly selected primary classifier declaration may change")
    old_samples = {item.sample_id: item.resolved_config_sha256 for item in previous.samples}
    if old_samples != {item.sample_id: item.resolved_config_sha256 for item in current.samples}:
        raise BatchTrainingError("Saved classifier selection must retain image preparation identities")
