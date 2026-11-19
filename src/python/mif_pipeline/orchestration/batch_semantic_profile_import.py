"""Owner validation and exact batch projections for explicit semantic profiles.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import yaml

from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from mif_pipeline.roi_validation import SemanticProfileV3

from .analysis_services import validate_semantic_profile_selectors
from .batch_analysis_preflight import MIF_PIPELINE_BATCH_RUNTIME_VERSION
from .batch_artifacts import (
    ArtifactProjectionTarget,
    ArtifactScope,
    BatchArtifactRef,
    StageAdapterRegistry,
    external_artifact_ref,
)
from .batch_manifest import (
    ReusableArtifactDeclaration,
    ValidatedBatchManifest,
    issue_validated_reusable_artifact,
)
from .batch_phenotyping_contracts import (
    MAX_ROI_SEMANTIC_PROFILE_BYTES,
    semantic_profile_identity_contract,
)

_VALIDATOR_VERSION = "mif-pipeline-semantic-profile-import/v1"


# ==============================================================================
#                         Batch semantic profile import
# ==============================================================================


class BatchSemanticProfileImportError(ValueError):
    """Explicit profile input lacks current, compatible owner validation."""


def _digest(value: object) -> str:
    return sha256(canonical_json_bytes(value)).hexdigest()


def _runtime_versions() -> dict[str, str]:
    return {"mif_pipeline": MIF_PIPELINE_BATCH_RUNTIME_VERSION}


def semantic_profile_compatibility_sha256(profile: SemanticProfileV3) -> str:
    """Return target-independent compatibility for this parsed configuration."""
    if not isinstance(profile, SemanticProfileV3):
        raise BatchSemanticProfileImportError("compatibility requires a parsed semantic profile")
    return _digest(
        {
            "schema_version": "mif-semantic-profile-compatibility/v1",
            "profile": profile.to_mapping(),
            "identity_contract_sha256": semantic_profile_identity_contract(int(profile.schema_version.rsplit("v", 1)[1])).contract_sha256,
            "validator_version": _VALIDATOR_VERSION,
            "runtime_versions": _runtime_versions(),
        }
    )


def read_semantic_profile_source(path: Path) -> tuple[bytes, SemanticProfileV3]:
    """Read one bounded criterion profile through the existing profile parser."""
    with path.open("rb") as stream:
        data = stream.read(MAX_ROI_SEMANTIC_PROFILE_BYTES + 1)
    if not data or len(data) > MAX_ROI_SEMANTIC_PROFILE_BYTES:
        raise BatchSemanticProfileImportError("semantic profile bytes must be nonempty and bounded")
    return data, SemanticProfileV3.from_mapping(yaml.safe_load(data.decode("utf-8")))


def author_missing_semantic_profile_declarations(
    manifest: ValidatedBatchManifest,
) -> tuple[ReusableArtifactDeclaration, ...]:
    """Author missing explicit profile inputs; issue no acceptance or receipts."""
    if not isinstance(manifest, ValidatedBatchManifest):
        raise BatchSemanticProfileImportError(
            "profile declaration authoring requires a validated manifest"
        )
    try:
        return _author_missing_declarations(manifest)
    except BatchSemanticProfileImportError:
        raise
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise BatchSemanticProfileImportError(
            f"semantic profile declaration authoring rejected: {exc}"
        ) from exc


def _author_missing_declarations(
    manifest: ValidatedBatchManifest,
) -> tuple[ReusableArtifactDeclaration, ...]:
    existing = tuple(
        item
        for item in manifest.manifest.reusable_artifacts
        if item.artifact_kind == "semantic_profile"
    )
    authored: list[ReusableArtifactDeclaration] = []
    used: set[str] = set()
    artifact_ids = {item.artifact_id for item in manifest.manifest.reusable_artifacts}
    contract = semantic_profile_identity_contract()
    for sample in manifest.enabled_samples:
        config = sample.config
        if not config.secondary_structures:
            continue
        if config.phenotyping.mode is None:
            raise BatchSemanticProfileImportError(
                f"sample {sample.sample_id}: semantic profiles require concrete phenotype "
                "selector settings; resolve the deferred settings before importing"
            )
        structures = {item.structure_id: item for item in config.secondary_structures}
        covered: set[str] = set()
        for reference in config.semantic.profile_references:
            path = reference.artifact_path
            if not path.is_absolute():
                path = config.output_dir / path
            path = path.resolve(strict=False)
            data, profile = read_semantic_profile_source(path)
            if profile.schema_version != "semantic-profile/v4":
                raise BatchSemanticProfileImportError("Revise the previous structure definition for the current region review.")
            raw_sha256 = sha256(data).hexdigest()
            if raw_sha256 != reference.content_sha256:
                raise BatchSemanticProfileImportError(
                    "semantic profile source bytes differ from configured reference"
                )
            if (profile.profile_id, profile.profile_version) != (
                reference.profile_id,
                reference.profile_version,
            ):
                raise BatchSemanticProfileImportError(
                    "semantic profile identity differs from configured reference"
                )
            structure = structures.get(profile.structure_id)
            if (
                structure is None
                or structure.semantic_profile != reference
                or profile.structure_id in covered
            ):
                raise BatchSemanticProfileImportError(
                    "semantic profile has foreign or duplicate structure coverage"
                )
            validate_semantic_profile_selectors(profile, config)
            identity = {
                "profile_id": profile.profile_id,
                "profile_version": profile.profile_version,
                "structure_id": profile.structure_id,
            }
            runtime = _runtime_versions()
            contract.validate(
                artifact_kind=contract.artifact_kind,
                schema_name=contract.schema_name,
                schema_version=contract.schema_version,
                owning_identity=identity,
                runtime_versions=runtime,
            )
            compatibility = semantic_profile_compatibility_sha256(profile)
            eligible = tuple(
                item
                for item in (*existing, *authored)
                if item.locator.resolve(strict=False) == path
                and item.validation_domain_id in (None, sample.validation_domain_id)
            )
            if len(eligible) > 1:
                raise BatchSemanticProfileImportError(
                    "semantic profile has duplicate eligible declarations"
                )
            if eligible:
                declaration = eligible[0]
                if (
                    declaration.schema_name != contract.schema_name
                    or declaration.schema_version != str(contract.schema_version)
                    or declaration.sha256 != raw_sha256
                    or declaration.byte_count != len(data)
                    or dict(declaration.owning_identity) != identity
                    or dict(declaration.runtime_versions) != runtime
                    or declaration.compatibility_signature_sha256 != compatibility
                ):
                    raise BatchSemanticProfileImportError(
                        "existing semantic profile declaration conflicts with configured profile"
                    )
            else:
                artifact_id = "semantic-profile-" + _digest(
                    {
                        "source_path": str(path),
                        "source_sha256": raw_sha256,
                        "validation_domain_id": sample.validation_domain_id,
                    }
                )
                if artifact_id in artifact_ids:
                    raise BatchSemanticProfileImportError(
                        "authored semantic profile ID conflicts with an existing resource"
                    )
                declaration = ReusableArtifactDeclaration(
                    artifact_id=artifact_id,
                    artifact_kind=contract.artifact_kind,
                    schema_name=contract.schema_name,
                    schema_version=str(contract.schema_version),
                    locator=path,
                    sha256=raw_sha256,
                    byte_count=len(data),
                    owning_identity=identity,
                    runtime_versions=runtime,
                    validation_domain_id=sample.validation_domain_id,
                    compatibility_signature_sha256=compatibility,
                )
                authored.append(declaration)
                artifact_ids.add(artifact_id)
            used.add(declaration.artifact_id)
            covered.add(profile.structure_id)
        if covered != set(structures):
            raise BatchSemanticProfileImportError(
                "semantic profile set lacks exact configured structure coverage"
            )
    if not {item.artifact_id for item in existing} <= used:
        raise BatchSemanticProfileImportError(
            "existing semantic profile declaration has no exact enabled sample target"
        )
    return tuple(sorted(authored, key=lambda item: item.artifact_id))


def import_batch_semantic_profiles(
    manifest: ValidatedBatchManifest,
    registry: StageAdapterRegistry,
    *, secondary_selections=None,
) -> tuple[BatchArtifactRef, ...]:
    """Validate only explicit profile declarations and issue exact sample refs."""
    if not isinstance(manifest, ValidatedBatchManifest) or not isinstance(
        registry, StageAdapterRegistry
    ):
        raise BatchSemanticProfileImportError(
            "profile import requires a validated manifest and stage registry"
        )
    try:
        return _import_profiles(manifest, registry, secondary_selections=secondary_selections)
    except BatchSemanticProfileImportError:
        raise
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise BatchSemanticProfileImportError(f"semantic profile import rejected: {exc}") from exc


def _import_profiles(
    manifest: ValidatedBatchManifest,
    registry: StageAdapterRegistry,
    *, secondary_selections=None,
) -> tuple[BatchArtifactRef, ...]:
    from .batch_secondary_definitions import resolve_secondary_structures, pending_group_profile_declarations
    from mif_pipeline.pipeline_core.config import AnalysisConfig

    selections = secondary_selections or {}
    unanswered_groups = {item.validation_domain_id for item in manifest.enabled_samples
                         if item.config.secondary_structures is None and item.sample_id not in selections}
    dormant = {item.artifact_id for group in unanswered_groups
               for item in pending_group_profile_declarations(manifest, group)}
    declarations = tuple(
        item
        for item in manifest.manifest.reusable_artifacts
        if item.artifact_kind == "semantic_profile" and item.artifact_id not in dormant
    )
    definitions = {
        item.sample_id: resolve_secondary_structures(item, selections.get(item.sample_id))
        for item in manifest.enabled_samples
        if item.config.secondary_structures is not None or item.sample_id in selections
    }
    samples = tuple(item for item in manifest.enabled_samples if definitions.get(item.sample_id))
    if not samples and not declarations:
        return ()
    contracts = tuple(
        item.identity_contract
        for spec in registry.specs
        for item in (*spec.required_inputs, *spec.optional_inputs)
        if item.artifact_kind == "semantic_profile"
    )
    if not contracts or any(item != semantic_profile_identity_contract() for item in contracts):
        raise BatchSemanticProfileImportError(
            "registered semantic profile input contracts are missing or incompatible"
        )
    contract = contracts[0]
    used = set()
    refs = []
    for sample in samples:
        selection = selections.get(sample.sample_id)
        config = (sample.config if selection is None
                  else AnalysisConfig.from_mapping(selection.context["validation_config"]))
        if config.phenotyping.mode is None and selection is None:
            raise BatchSemanticProfileImportError(
                f"sample {sample.sample_id}: semantic profile import requires concrete phenotype "
                "selector settings; resolve the deferred settings before importing"
            )
        structures = {item.structure_id: item for item in definitions[sample.sample_id]}
        covered = set()
        for reference in (item.semantic_profile for item in structures.values()):
            path = reference.artifact_path
            if not path.is_absolute():
                path = config.output_dir / path
            path = path.resolve(strict=False)
            candidates = tuple(
                item
                for item in declarations
                if item.locator.resolve(strict=False) == path
                and item.sha256 == reference.content_sha256
                and item.validation_domain_id in (None, sample.validation_domain_id)
            )
            if len(candidates) != 1:
                raise BatchSemanticProfileImportError(
                    "semantic profile requires one exact declaration per configured path/domain"
                )
            declaration = candidates[0]
            data, profile = read_semantic_profile_source(path)
            if profile.schema_version != "semantic-profile/v4":
                raise BatchSemanticProfileImportError("Revise the previous structure definition for the current region review.")
            raw_sha256 = sha256(data).hexdigest()
            if (
                raw_sha256 != declaration.sha256
                or raw_sha256 != reference.content_sha256
                or len(data) != declaration.byte_count
            ):
                raise BatchSemanticProfileImportError(
                    "semantic profile source bytes differ from declaration/configuration"
                )
            if (profile.profile_id, profile.profile_version) != (
                reference.profile_id,
                reference.profile_version,
            ):
                raise BatchSemanticProfileImportError(
                    "semantic profile identity differs from configured reference"
                )
            structure = structures.get(profile.structure_id)
            if (
                structure is None
                or structure.semantic_profile != reference
                or profile.structure_id in covered
            ):
                raise BatchSemanticProfileImportError(
                    "semantic profile has foreign or duplicate structure coverage"
                )
            identity = {
                "profile_id": profile.profile_id,
                "profile_version": profile.profile_version,
                "structure_id": profile.structure_id,
            }
            if (
                dict(declaration.owning_identity) != identity
                or dict(declaration.runtime_versions) != _runtime_versions()
            ):
                raise BatchSemanticProfileImportError(
                    "semantic profile declared identity/runtime differs from validated facts"
                )
            contract.validate(
                artifact_kind=declaration.artifact_kind,
                schema_name=declaration.schema_name,
                schema_version=int(declaration.schema_version),
                owning_identity=identity,
                runtime_versions=declaration.runtime_versions,
            )
            if declaration.compatibility_signature_sha256 != semantic_profile_compatibility_sha256(
                profile
            ):
                raise BatchSemanticProfileImportError(
                    "semantic profile compatibility signature differs from owner recipe"
                )
            validate_semantic_profile_selectors(
                profile, config, secondary_structures=tuple(structures.values()),
                defer_cell_selectors=selection is not None,
            )
            target = ArtifactProjectionTarget.from_manifest(
                manifest,
                (sample.sample_id,),
                ArtifactScope(
                    sample.validation_domain_id, sample.sample_id, sample.slide_id, None, ()
                ),
            )
            receipt = _digest(
                {
                    "schema_version": "mif-semantic-profile-validation/v1",
                    "validator_version": _VALIDATOR_VERSION,
                    "runtime_versions": _runtime_versions(),
                    "identity_contract_sha256": contract.contract_sha256,
                    "declaration_sha256": declaration.declaration_sha256,
                    "source_path": str(path),
                    "source_sha256": raw_sha256,
                    "byte_count": len(data),
                    "semantic_content_sha256": profile.content_sha256,
                    "owning_identity": identity,
                    "compatibility_signature_sha256": declaration.compatibility_signature_sha256,
                    "target": target.to_mapping(),
                    "phenotyping_group_id": sample.phenotyping_group_id,
                    "validated_selector_config_sha256": sample.resolved_config_sha256,
                }
            )
            accepted = issue_validated_reusable_artifact(
                manifest=manifest,
                declaration=declaration,
                owning_validation_receipt_sha256=receipt,
                producer_version=_VALIDATOR_VERSION,
                owning_identity_contract_sha256=contract.contract_sha256,
            )
            refs.append(
                external_artifact_ref(
                    accepted, target, manifest=manifest, identity_contract=contract
                )
            )
            used.add(declaration.artifact_id)
            covered.add(profile.structure_id)
        if covered != set(structures):
            raise BatchSemanticProfileImportError(
                "semantic profile set lacks exact configured structure coverage"
            )
    if used != {item.artifact_id for item in declarations}:
        raise BatchSemanticProfileImportError(
            "semantic profile declaration has no exact enabled sample/configuration target"
        )
    return tuple(sorted(refs, key=lambda item: item.artifact_ref_sha256))


__all__ = [
    "BatchSemanticProfileImportError",
    "author_missing_semantic_profile_declarations",
    "import_batch_semantic_profiles",
    "semantic_profile_compatibility_sha256",
]
