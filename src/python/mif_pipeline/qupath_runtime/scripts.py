"""Typed registry for version-controlled QuPath automation assets.

The `QuPathScriptBundle` record carries `root`, `bundle_sha256`, `assets`, `asset_sha256s`.

The runtime checks the declared project, image or process identity before exposing the
selected operation. Result records retain the native outcome and relevant file/saved-state
facts for the batch service to validate and publish."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
from importlib.resources import as_file, files
import json
import os
from pathlib import Path
import shutil
import tempfile

from mif_pipeline.pipeline_core.authority_layout import (
    AuthorityMemberRefV2,
    authority_member_ref_v2,
)
from mif_pipeline.pipeline_core.authority_provisioning import RetainedAuthorityLayoutV2
from mif_pipeline.pipeline_core.durable_records import DurableRecordError
from mif_pipeline.pipeline_core.durable_records import RuntimeAuthorityBindingV2
from mif_pipeline.pipeline_core.errors import ProjectPreparationError
from ._script_asset_registry import (
    INTERNAL_PROBE_PATHS,
    QuPathScriptAsset,
    registered_script_assets,
    relative_script_path,
)
from .project import ProjectLayout

_BUNDLE_MANIFEST = "qupath-script-bundle.v1.json"
_BUNDLE_SCHEMA = "qupath-script-bundle/v1"
_BUNDLE_EVIDENCE = "qupath-script-bundle.evidence.json"
_BUNDLE_EVIDENCE_SCHEMA = "qupath-script-bundle-evidence/v1"
_RUNTIME_ASSET_PACKAGE_VERSION = "mif-pipeline-0.1.0"
_RUNTIME_ASSET_VERSION = "qupath-assets-v1"
_PROJECT_STAGE_SCRIPT_BUNDLE_AUTHORITY_ISSUER = object()
_PROJECT_STAGE_BOOTSTRAP_INCLUDE = b"// MIF_PROJECT_STAGE_BOOTSTRAP_V1_INCLUDE\n"
_PROJECT_STAGE_BOOTSTRAP_TARGET = b"@@MIF_TARGET_SCRIPT_ASSET_ID@@"


# ==============================================================================
#                                    Scripts
# ==============================================================================


def _runtime_binding_sha256(value: object, *, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProjectPreparationError(f"{label} must be a SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class QuPathScriptBundle:
    """One content-addressed, project-owned set of registered Groovy bytes."""

    root: Path
    bundle_sha256: str
    assets: tuple[QuPathScriptAsset, ...]
    asset_sha256s: tuple[tuple[QuPathScriptAsset, str], ...]


class ProjectStageScriptBundleAuthority:
    """Issuer-only reopened content facts for one complete project bundle."""

    __slots__ = (
        "authority_layout_sha256",
        "workspace_storage_scope_sha256",
        "active_mutation_run_sha256",
        "project_uri",
        "project_identity_sha256",
        "script_bundle_sha256",
        "script_bundle_manifest_ref",
        "script_bundle_manifest_bytes",
        "script_bundle_manifest_sha256",
        "script_bundle_manifest_byte_count",
        "target_script_asset_id",
        "target_script_ref",
        "target_script_bytes",
        "target_script_sha256",
        "target_script_byte_count",
        "guard_script_asset_id",
        "guard_script_ref",
        "guard_script_bytes",
        "guard_script_sha256",
        "guard_script_byte_count",
        "_issuer_identity",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError(
            "project-stage script-bundle authority is issued only by project operations"
        )

    def __init_subclass__(cls, **kwargs: object) -> None:
        del kwargs
        raise TypeError("project-stage script-bundle authority cannot be subclassed")

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("project-stage script-bundle authority is immutable")

    @classmethod
    def _issue(
        cls,
        *,
        authority_layout_sha256: str,
        workspace_storage_scope_sha256: str,
        active_mutation_run_sha256: str,
        project_uri: str,
        project_identity_sha256: str,
        script_bundle_manifest_ref: AuthorityMemberRefV2,
        script_bundle_manifest_bytes: bytes,
        target_script_asset: QuPathScriptAsset,
        target_script_ref: AuthorityMemberRefV2,
        target_script_bytes: bytes,
        guard_script_asset: QuPathScriptAsset,
        guard_script_ref: AuthorityMemberRefV2,
        guard_script_bytes: bytes,
        _issuer: object,
    ) -> "ProjectStageScriptBundleAuthority":
        if _issuer is not _PROJECT_STAGE_SCRIPT_BUNDLE_AUTHORITY_ISSUER:
            raise TypeError("project-stage script-bundle issuer is invalid")
        digests = (
            authority_layout_sha256,
            workspace_storage_scope_sha256,
            active_mutation_run_sha256,
            project_identity_sha256,
        )
        if any(
            type(value) is not str
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in digests
        ):
            raise ProjectPreparationError("project-stage script-bundle authority digest is invalid")
        if (
            type(project_uri) is not str
            or not project_uri.startswith("file:")
            or type(script_bundle_manifest_bytes) is not bytes
            or not script_bundle_manifest_bytes
            or type(target_script_bytes) is not bytes
            or not target_script_bytes
            or type(guard_script_bytes) is not bytes
            or not guard_script_bytes
            or not isinstance(target_script_asset, QuPathScriptAsset)
            or not isinstance(guard_script_asset, QuPathScriptAsset)
            or target_script_asset is guard_script_asset
        ):
            raise ProjectPreparationError("project-stage script-bundle authority facts are invalid")
        manifest_sha256 = hashlib.sha256(script_bundle_manifest_bytes).hexdigest()
        target_sha256 = hashlib.sha256(target_script_bytes).hexdigest()
        guard_sha256 = hashlib.sha256(guard_script_bytes).hexdigest()
        for ref, family in (
            (script_bundle_manifest_ref, "batch_project_runtime_evidence"),
            (target_script_ref, "runtime_asset"),
            (guard_script_ref, "runtime_asset"),
        ):
            if (
                type(ref) is not AuthorityMemberRefV2
                or ref.authority_layout_sha256 != authority_layout_sha256
                or ref.family != family
            ):
                raise ProjectPreparationError("project-stage script-bundle retained ref is invalid")
        authority = object.__new__(cls)
        values = {
            "authority_layout_sha256": authority_layout_sha256,
            "workspace_storage_scope_sha256": workspace_storage_scope_sha256,
            "active_mutation_run_sha256": active_mutation_run_sha256,
            "project_uri": project_uri,
            "project_identity_sha256": project_identity_sha256,
            "script_bundle_sha256": manifest_sha256,
            "script_bundle_manifest_ref": script_bundle_manifest_ref,
            "script_bundle_manifest_bytes": script_bundle_manifest_bytes,
            "script_bundle_manifest_sha256": manifest_sha256,
            "script_bundle_manifest_byte_count": len(script_bundle_manifest_bytes),
            "target_script_asset_id": target_script_asset.value,
            "target_script_ref": target_script_ref,
            "target_script_bytes": target_script_bytes,
            "target_script_sha256": target_sha256,
            "target_script_byte_count": len(target_script_bytes),
            "guard_script_asset_id": guard_script_asset.value,
            "guard_script_ref": guard_script_ref,
            "guard_script_bytes": guard_script_bytes,
            "guard_script_sha256": guard_sha256,
            "guard_script_byte_count": len(guard_script_bytes),
            "_issuer_identity": _issuer,
        }
        for name, value in values.items():
            object.__setattr__(authority, name, value)
        return authority

    def _issued_by(self, issuer: object) -> bool:
        return issuer is self._issuer_identity


def publish_registered_script_asset(
    authority_layout: RetainedAuthorityLayoutV2,
    asset: QuPathScriptAsset,
) -> AuthorityMemberRefV2:
    """Publish and reopen one registered script by its exact retained ref."""

    if not isinstance(authority_layout, RetainedAuthorityLayoutV2):
        raise TypeError("authority_layout must be RetainedAuthorityLayoutV2")
    if not isinstance(asset, QuPathScriptAsset):
        raise TypeError("asset must be a QuPathScriptAsset")
    if asset not in registered_script_assets():
        raise ProjectPreparationError(f"QuPath script asset is not registered: {asset.value}")
    try:
        authority_layout.validate()
        payload = _source_bytes((asset,))[0][1]
        ref = authority_member_ref_v2(
            authority_layout.layout,
            family="runtime_asset",
            scope={
                "package_version": _RUNTIME_ASSET_PACKAGE_VERSION,
                "runtime_version": _RUNTIME_ASSET_VERSION,
            },
            natural_key={
                "asset_sha256": hashlib.sha256(payload).hexdigest(),
            },
        )
        bucket = authority_layout.bucket("immutable")
        bucket.publish_member(ref, payload)
        if bucket.read_member(ref) != payload:
            raise ProjectPreparationError(
                "registered QuPath runtime asset changed after publication"
            )
        return ref
    except (DurableRecordError, OSError) as exc:
        raise ProjectPreparationError(
            "registered QuPath runtime asset cannot be published safely"
        ) from exc


def catalogue_runtime_authority_binding_v2(
    authority_layout: RetainedAuthorityLayoutV2,
    *,
    workspace_storage_scope_sha256: str,
    active_mutation_run_sha256: str,
    role: str,
    request_identity_sha256: str,
    launch_nonce_sha256: str,
    binding: RuntimeAuthorityBindingV2 | None = None,
) -> RuntimeAuthorityBindingV2:
    """Create or authenticate the one closed catalogue runtime binding."""

    script_ref = publish_registered_script_asset(
        authority_layout,
        QuPathScriptAsset.UPSERT_BATCH_PROJECT_ENTRY,
    )
    exchange_scope = {
        "workspace_storage_scope_sha256": workspace_storage_scope_sha256,
        "active_mutation_run_sha256": active_mutation_run_sha256,
        "role": role,
        "request_identity_sha256": request_identity_sha256,
    }
    log_scope = {
        "workspace_storage_scope_sha256": workspace_storage_scope_sha256,
        "active_mutation_run_sha256": active_mutation_run_sha256,
        "launch_nonce_sha256": launch_nonce_sha256,
    }
    expected = RuntimeAuthorityBindingV2(
        authority_layout_sha256=authority_layout.layout.layout_sha256,
        workspace_storage_scope_sha256=workspace_storage_scope_sha256,
        script_ref=script_ref,
        request_ref=authority_member_ref_v2(
            authority_layout.layout,
            family="runtime_exchange",
            scope=exchange_scope,
            natural_key={"direction": "request"},
        ),
        evidence_ref=authority_member_ref_v2(
            authority_layout.layout,
            family="runtime_exchange",
            scope=exchange_scope,
            natural_key={"direction": "result"},
        ),
        stdout_ref=authority_member_ref_v2(
            authority_layout.layout,
            family="runtime_log",
            scope=log_scope,
            natural_key={"stream": "stdout"},
        ),
        stderr_ref=authority_member_ref_v2(
            authority_layout.layout,
            family="runtime_log",
            scope=log_scope,
            natural_key={"stream": "stderr"},
        ),
    )
    if binding is not None and (
        type(binding) is not RuntimeAuthorityBindingV2 or binding != expected
    ):
        raise ProjectPreparationError(
            "catalogue runtime authority binding differs from its exact context"
        )
    return expected


def training_runtime_authority_binding_v2(
    authority_layout: RetainedAuthorityLayoutV2,
    *,
    workspace_storage_scope_sha256: str,
    active_mutation_run_sha256: str,
    role: str,
    request_identity_sha256: str,
    launch_nonce_sha256: str,
    binding: RuntimeAuthorityBindingV2 | None = None,
) -> RuntimeAuthorityBindingV2:
    """Create or authenticate the closed native-classifier runtime binding."""

    if type(authority_layout) is not RetainedAuthorityLayoutV2:
        raise TypeError("authority_layout must be an exact RetainedAuthorityLayoutV2")
    if type(role) is not str or role != "classifier_training":
        raise ProjectPreparationError("training runtime role must be classifier_training")
    workspace_storage_scope_sha256 = _runtime_binding_sha256(
        workspace_storage_scope_sha256,
        label="workspace_storage_scope_sha256",
    )
    active_mutation_run_sha256 = _runtime_binding_sha256(
        active_mutation_run_sha256,
        label="active_mutation_run_sha256",
    )
    request_identity_sha256 = _runtime_binding_sha256(
        request_identity_sha256,
        label="request_identity_sha256",
    )
    launch_nonce_sha256 = _runtime_binding_sha256(
        launch_nonce_sha256,
        label="launch_nonce_sha256",
    )
    if binding is not None and type(binding) is not RuntimeAuthorityBindingV2:
        raise TypeError("binding must be an exact RuntimeAuthorityBindingV2")

    script_ref = publish_registered_script_asset(
        authority_layout,
        QuPathScriptAsset.TRAIN_NATIVE_CLASSIFIER,
    )
    exchange_scope = {
        "workspace_storage_scope_sha256": workspace_storage_scope_sha256,
        "active_mutation_run_sha256": active_mutation_run_sha256,
        "role": role,
        "request_identity_sha256": request_identity_sha256,
    }
    log_scope = {
        "workspace_storage_scope_sha256": workspace_storage_scope_sha256,
        "active_mutation_run_sha256": active_mutation_run_sha256,
        "launch_nonce_sha256": launch_nonce_sha256,
    }
    expected = RuntimeAuthorityBindingV2(
        authority_layout_sha256=authority_layout.layout.layout_sha256,
        workspace_storage_scope_sha256=workspace_storage_scope_sha256,
        script_ref=script_ref,
        request_ref=authority_member_ref_v2(
            authority_layout.layout,
            family="runtime_exchange",
            scope=exchange_scope,
            natural_key={"direction": "request"},
        ),
        evidence_ref=authority_member_ref_v2(
            authority_layout.layout,
            family="runtime_exchange",
            scope=exchange_scope,
            natural_key={"direction": "result"},
        ),
        stdout_ref=authority_member_ref_v2(
            authority_layout.layout,
            family="runtime_log",
            scope=log_scope,
            natural_key={"stream": "stdout"},
        ),
        stderr_ref=authority_member_ref_v2(
            authority_layout.layout,
            family="runtime_log",
            scope=log_scope,
            natural_key={"stream": "stderr"},
        ),
    )
    if binding is not None and binding != expected:
        raise ProjectPreparationError(
            "training runtime authority binding differs from its exact context"
        )
    return expected


def held_out_evaluation_runtime_authority_binding_v2(
    authority_layout: RetainedAuthorityLayoutV2,
    *,
    workspace_storage_scope_sha256: str,
    active_mutation_run_sha256: str,
    role: str,
    fit_material_sha256: str,
    request_identity_sha256: str,
    launch_nonce_sha256: str,
    binding: RuntimeAuthorityBindingV2 | None = None,
) -> RuntimeAuthorityBindingV2:
    """Create or authenticate the fixed held-out evaluation binding."""

    if type(authority_layout) is not RetainedAuthorityLayoutV2:
        raise TypeError("authority_layout must be an exact RetainedAuthorityLayoutV2")
    if type(role) is not str or role != "classifier_held_out_evaluation":
        raise ProjectPreparationError(
            "held-out evaluation runtime role must be classifier_held_out_evaluation"
        )
    workspace_storage_scope_sha256 = _runtime_binding_sha256(
        workspace_storage_scope_sha256,
        label="workspace_storage_scope_sha256",
    )
    active_mutation_run_sha256 = _runtime_binding_sha256(
        active_mutation_run_sha256,
        label="active_mutation_run_sha256",
    )
    fit_material_sha256 = _runtime_binding_sha256(
        fit_material_sha256,
        label="fit_material_sha256",
    )
    request_identity_sha256 = _runtime_binding_sha256(
        request_identity_sha256,
        label="request_identity_sha256",
    )
    launch_nonce_sha256 = _runtime_binding_sha256(
        launch_nonce_sha256,
        label="launch_nonce_sha256",
    )
    if binding is not None and type(binding) is not RuntimeAuthorityBindingV2:
        raise TypeError("binding must be an exact RuntimeAuthorityBindingV2")

    script_ref = publish_registered_script_asset(
        authority_layout,
        QuPathScriptAsset.EVALUATE_HELD_OUT_CLASSIFIERS,
    )
    exchange_scope = {
        "workspace_storage_scope_sha256": workspace_storage_scope_sha256,
        "active_mutation_run_sha256": active_mutation_run_sha256,
        "role": role,
        "fit_material_sha256": fit_material_sha256,
        "request_identity_sha256": request_identity_sha256,
    }

    def exchange_ref(direction: str) -> AuthorityMemberRefV2:
        key_sha256 = hashlib.sha256(
            _canonical_json_bytes(
                {
                    "authority_layout_sha256": authority_layout.layout.layout_sha256,
                    "family": "runtime_exchange",
                    "scope": exchange_scope,
                    "natural_key": {"direction": direction},
                }
            )
        ).hexdigest()
        return AuthorityMemberRefV2(
            authority_layout_sha256=authority_layout.layout.layout_sha256,
            family="runtime_exchange",
            key_sha256=key_sha256,
            suffix="json",
            media_type="application/json",
        )

    log_scope = {
        "workspace_storage_scope_sha256": workspace_storage_scope_sha256,
        "active_mutation_run_sha256": active_mutation_run_sha256,
        "launch_nonce_sha256": launch_nonce_sha256,
    }
    expected = RuntimeAuthorityBindingV2(
        authority_layout_sha256=authority_layout.layout.layout_sha256,
        workspace_storage_scope_sha256=workspace_storage_scope_sha256,
        script_ref=script_ref,
        request_ref=exchange_ref("request"),
        evidence_ref=exchange_ref("result"),
        stdout_ref=authority_member_ref_v2(
            authority_layout.layout,
            family="runtime_log",
            scope=log_scope,
            natural_key={"stream": "stdout"},
        ),
        stderr_ref=authority_member_ref_v2(
            authority_layout.layout,
            family="runtime_log",
            scope=log_scope,
            natural_key={"stream": "stderr"},
        ),
    )
    if binding is not None and binding != expected:
        raise ProjectPreparationError(
            "held-out evaluation runtime authority binding differs from its exact context"
        )
    return expected


def script_path(project: ProjectLayout, asset: QuPathScriptAsset) -> Path:
    """Resolve one asset from the complete immutable project bundle."""
    if not isinstance(project, ProjectLayout):
        raise TypeError("project must be a ProjectLayout")
    if not isinstance(asset, QuPathScriptAsset):
        raise TypeError("asset must be a QuPathScriptAsset")
    bundle = materialize_script_bundle(project, registered_script_assets())
    return bundle_script_path(bundle, asset)


def _script_bundle_manifest(
    assets: tuple[QuPathScriptAsset, ...],
) -> tuple[
    tuple[QuPathScriptAsset, ...],
    tuple[tuple[QuPathScriptAsset, bytes], ...],
    dict[str, object],
]:
    canonical_assets = _canonical_assets(assets)
    sources = _source_bytes(canonical_assets)
    manifest: dict[str, object] = {
        "schema_version": _BUNDLE_SCHEMA,
        "assets": [
            {
                "path": _relative_script_path(asset).as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for asset, content in sources
        ],
    }
    return canonical_assets, sources, manifest


def registered_script_bundle_sha256() -> str:
    """Return the content digest of the complete registered script bundle."""

    _assets, _sources, manifest = _script_bundle_manifest(registered_script_assets())
    return hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest()


def registered_script_asset_sha256(asset: QuPathScriptAsset) -> str:
    """Return the immutable source-byte digest for one registered asset."""

    if not isinstance(asset, QuPathScriptAsset):
        raise TypeError("asset must be a QuPathScriptAsset")
    if asset not in registered_script_assets():
        raise ProjectPreparationError(f"QuPath script asset is not registered: {asset.value}")
    payload = _source_bytes((asset,))[0][1]
    return hashlib.sha256(payload).hexdigest()


def authenticate_registered_script_bundle(
    project: ProjectLayout,
    *,
    expected_bundle_sha256: str,
) -> QuPathScriptBundle:
    """Reopen the complete registered bundle and its project evidence, read-only."""

    if type(project) is not ProjectLayout:
        raise ProjectPreparationError("registered bundle requires its exact project")
    expected_bundle_sha256 = _runtime_binding_sha256(
        expected_bundle_sha256, label="expected_bundle_sha256"
    )
    assets, sources, manifest = _script_bundle_manifest(registered_script_assets())
    digest = hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest()
    if digest != expected_bundle_sha256:
        raise ProjectPreparationError("registered bundle source bytes changed")
    bundle = QuPathScriptBundle(
        root=project.scripts_dir / f"bundle-{digest}",
        bundle_sha256=digest,
        assets=assets,
        asset_sha256s=tuple(
            (asset, hashlib.sha256(content).hexdigest()) for asset, content in sources
        ),
    )
    _validate_published_bundle(project, bundle, manifest)
    from .catalogue import (
        ProjectAuthorityError,
        _bounded_registration_tree_observation,
        _read_bounded_relative_regular_file,
    )

    expected_files = {
        _relative_script_path(asset).as_posix(): content for asset, content in sources
    }
    expected_files[_BUNDLE_MANIFEST] = _canonical_json_bytes(manifest)
    expected_directories = {
        parent.as_posix()
        for relative in expected_files
        for parent in Path(relative).parents
        if parent != Path(".")
    }
    expected_members = tuple(
        sorted(
            [{"path": name, "kind": "directory"} for name in expected_directories]
            + [
                {
                    "path": name,
                    "kind": "file",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "byte_count": len(payload),
                }
                for name, payload in expected_files.items()
            ],
            key=lambda item: str(item["path"]),
        )
    )
    try:
        observed = _bounded_registration_tree_observation(bundle.root)
        evidence = _read_bounded_relative_regular_file(
            project.data_dir,
            Path(_BUNDLE_EVIDENCE),
            max_bytes=1024 * 1024,
            label="registered script bundle evidence",
        )
    except ProjectAuthorityError as exc:
        raise ProjectPreparationError("registered script bundle cannot be reopened") from exc
    if observed.members != expected_members or observed.excluded_files:
        raise ProjectPreparationError("registered script bundle tree differs")
    expected_evidence = _bundle_evidence_document(project, bundle, manifest)
    if evidence != (
        json.dumps(expected_evidence, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    ):
        raise ProjectPreparationError("registered script bundle evidence differs")
    return bundle


def materialize_script_bundle(
    project: ProjectLayout,
    assets: tuple[QuPathScriptAsset, ...],
) -> QuPathScriptBundle:
    """Atomically publish and revalidate one digest-addressed script bundle."""
    if not isinstance(project, ProjectLayout):
        raise TypeError("project must be a ProjectLayout")
    canonical_assets, sources, manifest = _script_bundle_manifest(assets)
    bundle_sha256 = hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest()
    root = project.scripts_dir / f"bundle-{bundle_sha256}"
    bundle = QuPathScriptBundle(
        root=root,
        bundle_sha256=bundle_sha256,
        assets=canonical_assets,
        asset_sha256s=tuple(
            (asset, item["sha256"])
            for (asset, _content), item in zip(sources, manifest["assets"], strict=True)
        ),
    )
    if root.exists() or root.is_symlink():
        _validate_published_bundle(project, bundle, manifest)
        _publish_complete_bundle_evidence(project, bundle, manifest)
        return bundle

    project.scripts_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{root.name}.staging-", dir=project.scripts_dir))
    try:
        for asset, content in sources:
            target = staging / _relative_script_path(asset)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (staging / _BUNDLE_MANIFEST).write_bytes(_canonical_json_bytes(manifest))
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                with path.open("r+b") as stream:
                    os.fsync(stream.fileno())
        try:
            os.replace(staging, root)
        except OSError:
            if not root.is_dir():
                raise
        _validate_published_bundle(project, bundle, manifest)
        _publish_complete_bundle_evidence(project, bundle, manifest)
        return bundle
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def bundle_script_path(
    bundle: QuPathScriptBundle,
    asset: QuPathScriptAsset,
) -> Path:
    """Resolve a registered member of one immutable bundle."""
    if not isinstance(bundle, QuPathScriptBundle):
        raise TypeError("bundle must be a QuPathScriptBundle")
    if not isinstance(asset, QuPathScriptAsset):
        raise TypeError("asset must be a QuPathScriptAsset")
    if asset not in bundle.assets:
        raise ProjectPreparationError(f"QuPath script asset is absent from bundle: {asset.value}")
    return bundle.root / _relative_script_path(asset)


def materialize_evidence_script(destination: Path, asset: QuPathScriptAsset) -> Path:
    """Copies one registered read-only operation into a fresh computation directory.

    The evidence runner hashes this exact script before and after execution.
    We require a new destination so a concurrent operation cannot replace the
    launched program. Scientific outputs remain separately authenticated by
    their domain owner; this helper does not create a stage mutation authority.
    """
    if not isinstance(asset, QuPathScriptAsset):
        raise TypeError("asset must be a registered QuPathScriptAsset")
    payload = _source_bytes((asset,))[0][1]
    path = Path(destination) / Path(asset.value).name
    with path.open("xb") as stream:
        stream.write(payload)
    return path


def copy_script_assets(destination: Path) -> None:
    """Copy every registered source script while preserving stage paths."""
    target_root = Path(destination)
    for asset, content in _source_bytes(registered_script_assets()):
        target = target_root / _relative_script_path(asset)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if not target.is_file() or target.read_bytes() != content:
                raise ProjectPreparationError(f"conflicting QuPath script asset: {target}")
            continue
        target.write_bytes(content)


def _canonical_assets(
    assets: tuple[QuPathScriptAsset, ...],
) -> tuple[QuPathScriptAsset, ...]:
    if not isinstance(assets, tuple) or not assets:
        raise TypeError("assets must be a non-empty tuple of QuPathScriptAsset")
    if any(not isinstance(asset, QuPathScriptAsset) for asset in assets):
        raise TypeError("assets must contain only QuPathScriptAsset")
    if len(set(assets)) != len(assets):
        raise ProjectPreparationError("QuPath script bundle assets must be unique")
    selected = set(assets)
    return tuple(asset for asset in registered_script_assets() if asset in selected)


def _source_bytes(
    assets: tuple[QuPathScriptAsset, ...],
) -> tuple[tuple[QuPathScriptAsset, bytes], ...]:
    """Prepare executable scripts from their single maintained source.

    The project-stage scripts include one shared startup template. We resolve
    that template once for this preparation and insert the operation's asset
    ID before calculating bundle digests or publishing executable files.
    Their scientific bodies and the native project guard retain their existing
    execution order. Installed builds resolve the same registered resources.
    """
    with ExitStack() as stack:
        values: list[tuple[QuPathScriptAsset, bytes]] = []
        missing: list[str] = []
        bootstrap: bytes | None = None
        for asset in assets:
            source = _source_script_path(asset, stack=stack)
            if source is None:
                missing.append(Path(asset.value).name)
                continue
            content = source.read_bytes()
            if content.startswith(_PROJECT_STAGE_BOOTSTRAP_INCLUDE):
                if bootstrap is None:
                    template = _source_script_path(
                        QuPathScriptAsset.PROJECT_STAGE_BOOTSTRAP, stack=stack
                    )
                    if template is None:
                        raise ProjectPreparationError(
                            "required QuPath script is missing: project_stage_bootstrap.groovy"
                        )
                    bootstrap = template.read_bytes()
                content = bootstrap.replace(
                    _PROJECT_STAGE_BOOTSTRAP_TARGET, asset.value.encode("utf-8")
                ) + content[len(_PROJECT_STAGE_BOOTSTRAP_INCLUDE):]
            values.append((asset, content))
    if missing:
        raise ProjectPreparationError("required QuPath script is missing: " + ", ".join(missing))
    return tuple(values)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _validate_published_bundle(
    project: ProjectLayout,
    bundle: QuPathScriptBundle,
    expected_manifest: dict[str, object],
) -> None:
    """Validate published bundle for the current operation.

    The implementation compares the supplied record identities and content digests with the
    operation context. A mismatch is reported at this boundary before the record is accepted
    by its caller."""

    root = bundle.root
    if (
        root.parent != project.scripts_dir
        or any(
            path.is_symlink() or path.resolve() != path.absolute()
            for path in (project.root, project.scripts_dir, root)
        )
        or root.parent.is_symlink()
        or not root.parent.is_dir()
        or root.is_symlink()
        or not root.is_dir()
    ):
        raise ProjectPreparationError(
            f"immutable QuPath script bundle is not a project-owned directory: {root}"
        )
    manifest_path = root / _BUNDLE_MANIFEST
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ProjectPreparationError(
            f"immutable QuPath script bundle manifest is not a regular file: {manifest_path}"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectPreparationError(
            f"immutable QuPath script bundle is unreadable: {root}"
        ) from exc
    expected_files = {_relative_script_path(asset) for asset in bundle.assets} | {
        Path(_BUNDLE_MANIFEST)
    }
    expected_directories = {
        parent for relative in expected_files for parent in relative.parents if parent != Path(".")
    }
    expected_paths = expected_files | expected_directories
    actual_paths = {path.relative_to(root) for path in root.rglob("*")}
    invalid_directories = any(
        path.is_symlink() or not path.is_dir()
        for relative in expected_directories
        for path in (root / relative,)
    )
    invalid_files = any(
        path.is_symlink() or not path.is_file()
        for relative in expected_files
        for path in (root / relative,)
    )
    if (
        manifest != expected_manifest
        or hashlib.sha256(manifest_path.read_bytes()).hexdigest() != bundle.bundle_sha256
        or actual_paths != expected_paths
        or invalid_directories
        or invalid_files
    ):
        raise ProjectPreparationError(
            f"immutable QuPath script bundle contains mixed or missing files: {root}"
        )
    for asset, expected_sha256 in bundle.asset_sha256s:
        path = bundle_script_path(bundle, asset)
        relative = path.relative_to(root)
        has_symlink = path.is_symlink() or any(
            (root / parent).is_symlink() for parent in relative.parents if parent != Path(".")
        )
        if (
            has_symlink
            or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256
        ):
            raise ProjectPreparationError(f"immutable QuPath script bundle digest mismatch: {path}")


def _bundle_evidence_document(
    project: ProjectLayout,
    bundle: QuPathScriptBundle,
    manifest: dict[str, object],
) -> dict[str, object]:
    manifest_path = bundle.root / _BUNDLE_MANIFEST
    return {
        "schema_version": _BUNDLE_EVIDENCE_SCHEMA,
        "project_file": str(project.project_file.resolve()),
        "bundle_sha256": bundle.bundle_sha256,
        "bundle_path": str(bundle.root.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "manifest_sha256": hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest(),
        "assets": manifest["assets"],
    }


def _publish_complete_bundle_evidence(
    project: ProjectLayout,
    bundle: QuPathScriptBundle,
    manifest: dict[str, object],
) -> None:
    if bundle.assets != registered_script_assets():
        return
    evidence_path = project.data_dir / _BUNDLE_EVIDENCE
    expected = _bundle_evidence_document(project, bundle, manifest)
    if (
        project.data_dir.parent != project.root
        or any(
            path.is_symlink() or path.resolve() != path.absolute()
            for path in (project.root, project.data_dir, evidence_path)
        )
        or not project.data_dir.is_dir()
        or evidence_path.is_symlink()
        or (evidence_path.exists() and not evidence_path.is_file())
    ):
        raise ProjectPreparationError(
            f"QuPath script bundle evidence is not a regular project file: {evidence_path}"
        )
    # This mutable pointer is derived from the bundle verified immediately before
    # publication. Rebuild it after relocation or corruption; historical bundles
    # and their retained authority records remain immutable.
    payload = json.dumps(expected, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    if (
        evidence_path.exists()
        and evidence_path.stat().st_size == len(payload)
        and evidence_path.read_bytes() == payload
    ):
        return
    _atomic_write_bytes(evidence_path, payload)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, native_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(native_temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _relative_script_path(asset: QuPathScriptAsset) -> Path:
    return Path(relative_script_path(asset))


def _source_script_path(
    asset: QuPathScriptAsset,
    *,
    stack: ExitStack,
) -> Path | None:
    return _source_relative_script_path(_relative_script_path(asset), stack=stack)


def _source_relative_script_path(relative: Path, *, stack: ExitStack) -> Path | None:
    checkout_root = _repository_checkout_root()
    if checkout_root is not None:
        repository_source = checkout_root / "qupath" / "scripts" / relative
        if repository_source.is_file():
            return repository_source
    resource = files("mif_pipeline.qupath_runtime.assets").joinpath(*relative.parts)
    if resource.is_file():
        return Path(stack.enter_context(as_file(resource)))
    return None


def internal_probe_bytes(relative: str) -> bytes:
    """Read our authored checkout probe or its generated installed resource."""
    if relative not in INTERNAL_PROBE_PATHS:
        raise ProjectPreparationError("internal QuPath probe is not registered")
    with ExitStack() as stack:
        source = _source_relative_script_path(Path(relative), stack=stack)
        if source is None:
            raise ProjectPreparationError("required internal QuPath probe is missing: " + relative)
        return source.read_bytes()


def _repository_checkout_root() -> Path | None:
    module_path = Path(__file__).resolve()
    checkout_root = module_path.parents[4]
    expected = checkout_root / "src" / "python" / "mif_pipeline" / "qupath_runtime" / "scripts.py"
    if not expected.is_file() or expected.resolve() != module_path:
        return None
    if not (checkout_root / "qupath" / "scripts").is_dir():
        return None
    return checkout_root


__all__ = [
    "ProjectStageScriptBundleAuthority",
    "QuPathScriptAsset",
    "QuPathScriptBundle",
    "authenticate_registered_script_bundle",
    "bundle_script_path",
    "catalogue_runtime_authority_binding_v2",
    "copy_script_assets",
    "held_out_evaluation_runtime_authority_binding_v2",
    "internal_probe_bytes",
    "materialize_script_bundle",
    "publish_registered_script_asset",
    "registered_script_asset_sha256",
    "registered_script_bundle_sha256",
    "script_path",
    "training_runtime_authority_binding_v2",
]
