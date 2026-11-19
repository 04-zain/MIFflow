"""State-owned setup for existing-project Warpy registration evidence.

The `RegistrationProjectPreflight` record carries `disposition`, `snapshot_resolver`,
`snapshot`, `outer_request`, `runtime_target_plan` and its remaining declared fields.

The composition binds the selected stage inputs to the current analysis operation and
delegates scientific work to its owning implementation. Recorded results retain the exact
target, configuration and accepted upstream references."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Literal
from urllib.parse import urlsplit
from urllib.request import url2pathname

from mif_pipeline.pipeline_core.durable_records import canonical_json_bytes
from mif_pipeline.pipeline_core.stage_result import InteractionReason, StageResult
from mif_pipeline.qupath_runtime.project_content import same_project_snapshot
from mif_pipeline.qupath_runtime.catalogue import (
    ProjectMutationAcquisitionSession,
    ProjectMutationArtifactInventory,
    ProjectMutationArtifactTarget,
    ProjectMutationLeaseProvider,
)
from mif_pipeline.qupath_runtime.project import ProjectLayout
from mif_pipeline.qupath_runtime.registration_session import (
    CurrentRegistrationProjectSnapshot,
    CurrentRegistrationProjectSnapshotResolver,
    RegistrationRuntimeTargetPlanSeed,
)
from mif_pipeline.qupath_runtime.runner import (
    QuPathRegistrationRuntimeTargetPlan,
    QuPathRunner,
)

from .batch_artifacts import BatchCurrentExecutionAuthority
from .batch_project import (
    BatchProjectCatalogueStore,
    BatchRegistrationProjectRequest,
)
from .batch_registration import (
    RegistrationExecutionRoute,
    authenticate_registration_execution_route,
)
from .batch_state import BatchStateStore

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
RegistrationRole = Literal["registration_import", "transfer_qc"]
_PREFLIGHT_ISSUER = object()


# ==============================================================================
#                           Batch registration session
# ==============================================================================


class BatchRegistrationSessionError(ValueError):
    """Registration preflight material violates its current closed contract."""


def _sha(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise BatchRegistrationSessionError(f"{label} must be one lowercase SHA-256")
    return value


def _digest(value: object) -> str:
    try:
        return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
    except Exception as exc:
        raise BatchRegistrationSessionError(
            "registration preflight material is not canonical JSON"
        ) from exc


@dataclass(frozen=True, slots=True, init=False)
class RegistrationProjectPreflight:
    """State-issued image-pair choice or acquisition-ready capability."""

    disposition: Literal["transform_choice_required", "acquisition_ready"]
    snapshot_resolver: CurrentRegistrationProjectSnapshotResolver
    snapshot: CurrentRegistrationProjectSnapshot
    outer_request: BatchRegistrationProjectRequest | None
    runtime_target_plan: QuPathRegistrationRuntimeTargetPlan | None
    acquisition_session: ProjectMutationAcquisitionSession | None
    recovery_record_sha256: str | None
    _issuer: object

    def __new__(cls, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("registration project preflight is state-issued")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("registration project preflight is nonserializable")

    @classmethod
    def _issue(cls, **values: object) -> "RegistrationProjectPreflight":
        disposition = values.get("disposition")
        resolver = values.get("snapshot_resolver")
        snapshot = values.get("snapshot")
        request = values.get("outer_request")
        target_plan = values.get("runtime_target_plan")
        session = values.get("acquisition_session")
        if (
            disposition not in {"transform_choice_required", "acquisition_ready"}
            or type(resolver) is not CurrentRegistrationProjectSnapshotResolver
            or type(snapshot) is not CurrentRegistrationProjectSnapshot
        ):
            raise BatchRegistrationSessionError("registration project preflight inputs are invalid")
        if disposition == "transform_choice_required":
            if any(
                value is not None for value in (request, target_plan, session)
            ):
                raise BatchRegistrationSessionError(
                    "transform-choice preflight cannot claim acquisition authority"
                )
        elif (
            snapshot.transform.presence != "present"
            or type(request) is not BatchRegistrationProjectRequest
            or type(target_plan) is not QuPathRegistrationRuntimeTargetPlan
            or type(session) is not ProjectMutationAcquisitionSession
            or request.snapshot_resolution_sha256 != snapshot.snapshot_resolution_sha256
            or request.runtime_target_plan_sha256 != target_plan.target_plan_sha256
            or request.registration_exchange_id != target_plan.registration_exchange_id
        ):
            raise BatchRegistrationSessionError("acquisition-ready preflight authority disagrees")
        values.setdefault("recovery_record_sha256", None)
        if values["recovery_record_sha256"] is not None:
            _sha(values["recovery_record_sha256"], "recovery_record_sha256")
        preflight = object.__new__(cls)
        for name in cls.__dataclass_fields__:
            object.__setattr__(
                preflight,
                name,
                _PREFLIGHT_ISSUER if name == "_issuer" else values[name],
            )
        return preflight


def build_registration_transform_interaction(
    route: RegistrationExecutionRoute,
    preflight: RegistrationProjectPreflight,
) -> StageResult:
    """Retain the observed transform choice for one exact operator response."""

    route = authenticate_registration_execution_route(route)
    if (
        route.disposition != "warpy_r2"
        or type(preflight) is not RegistrationProjectPreflight
        or preflight._issuer is not _PREFLIGHT_ISSUER
        or preflight.disposition != "transform_choice_required"
    ):
        raise BatchRegistrationSessionError(
            "transform interaction requires its exact image-pair preflight"
        )
    snapshot = preflight.snapshot
    _validate_current_snapshot(route, snapshot)
    sample, tissue = route.sample, route.tissue_result
    if sample is None or tissue is None:
        raise BatchRegistrationSessionError("transform interaction lacks sample or tissue scope")
    return StageResult.needs_interaction(
        stage="registration",
        slide_id=sample.slide_id,
        generation_id=tissue.generation_id,
        reason=InteractionReason.WARPY_REGISTRATION_REQUIRED,
        affected_object_ids=tissue.section_ids,
        message=(
            "Choose automatic alignment or use landmarks to align the paired image with mIF. "
            "The result will be reviewed before annotation guidance is enabled."
        ),
        allowed_actions=("import_warpy_transform", "side_by_side", "skip_moving_reference"),
        resume_context={
            "schema_version": "warpy-registration-interaction/v1",
            "project_file": url2pathname(urlsplit(snapshot.project_uri).path),
            "moving_project_entry_id": snapshot.moving_entry.project_entry_id,
            "fixed_project_entry_id": snapshot.fixed_entry.project_entry_id,
            "expected_transform_file": url2pathname(
                urlsplit(snapshot.transform.transform_uri).path
            ),
            "moving_image_id": (route.moving_authority.source_image_id
                or sample.config.registration.moving_image_id or route.moving_authority.reference_id),
            "fixed_image_id": tissue.canonical_image_id,
            "fixed_channels": [dict(channel) for channel in tissue.artifact_ref.owning_identity["canonical_channels"]],
            "moving_channels": list(preflight.snapshot_resolver.moving_channel_names),
            "moving_is_rgb": preflight.snapshot_resolver.moving_is_rgb,
            "batch_registration_snapshot": snapshot.to_mapping(),
        },
        checkpoint_id="pending-workflow-checkpoint",
    )


def _snapshot_transition_is_exact(
    previous: CurrentRegistrationProjectSnapshot,
    current: CurrentRegistrationProjectSnapshot,
) -> bool:
    """Join the retained choice to the fresh same-pair transform observation.

    Complete project bytes and inode can change on a display-only QuPath save.
    The existing retained project-content comparison authenticates that narrow
    difference; source, geometry, calibration and saved hierarchy remain exact.
    The entry directory also contains fitting logs and the new transform receipt,
    so its aggregate digest is not an unchanged-input requirement. The fresh
    provider snapshot is validated separately before this join.
    """
    if (
        type(previous) is not CurrentRegistrationProjectSnapshot
        or previous.transform.presence not in {"absent", "present"}
        or current.transform.presence != "present"
        or current.transform.content_sha256 is None
        or previous.transform.transform_uri != current.transform.transform_uri
        or previous.transform.fixed_project_entry_id != current.transform.fixed_project_entry_id
        or previous.transform.moving_project_entry_id != current.transform.moving_project_entry_id
        or previous.project_uri != current.project_uri
    ):
        return False
    if not same_project_snapshot(
        Path(url2pathname(urlsplit(current.project_uri).path)),
        previous.qpproj_sha256, current.qpproj_sha256,
    ):
        return False
    before = previous.to_mapping()
    after = current.to_mapping()
    before.pop("snapshot_resolution_sha256")
    after.pop("snapshot_resolution_sha256")
    before.pop("transform")
    after.pop("transform")
    for key in ("qpproj_sha256", "qpproj_file_identity"):
        before.pop(key)
        after.pop(key)
    for observation in (before, after):
        for role in ("fixed_entry", "moving_entry"):
            entry = observation[role]
            entry.pop("entry_observation_sha256")
            entry["members"] = [
                member for member in entry["members"]
                if member["target_kind"] != "entry_hierarchy_tree"
            ]
    return before == after


def _registration_static_targets(
    *,
    snapshot: CurrentRegistrationProjectSnapshot,
    target_plan: QuPathRegistrationRuntimeTargetPlan,
    project_provider: ProjectMutationLeaseProvider,
    state_store: BatchStateStore,
    project: ProjectLayout,
) -> tuple[ProjectMutationArtifactTarget, ...]:
    def predecessor(path: Path) -> str | None:
        if path.is_symlink():
            raise BatchRegistrationSessionError("registration control target is a symbolic link")
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

    state_layout = getattr(state_store, "layout", None)
    if state_layout is None:
        raise BatchRegistrationSessionError("registration state layout is unavailable")
    qpproj = project.project_file.absolute()
    targets: list[ProjectMutationArtifactTarget] = [
        ProjectMutationArtifactTarget.create(
            target_kind="registration_project_snapshot",
            resolution_kind="authority_identity_relative_from_provider",
            owner_root_uri=project_provider.authority_root.as_uri(),
            relative_locator_or_metadata_key=(
                "registration-snapshots/" f"{snapshot.snapshot_resolution_sha256}.json"
            ),
            predecessor_sha256=snapshot.snapshot_resolution_sha256,
        ),
        ProjectMutationArtifactTarget.create(
            target_kind="qpproj",
            resolution_kind="exact_uri",
            owner_root_uri=qpproj.parent.as_uri(),
            relative_locator_or_metadata_key=qpproj.name,
            predecessor_sha256=snapshot.qpproj_sha256,
        ),
    ]
    for entry in (snapshot.fixed_entry, snapshot.moving_entry):
        for member in entry.members:
            targets.append(
                ProjectMutationArtifactTarget.create(
                    target_kind=member.target_kind,
                    resolution_kind=("entry_member_from_current_project_snapshot"),
                    owner_root_uri=snapshot.project_uri,
                    relative_locator_or_metadata_key=member.semantic_locator,
                    predecessor_sha256=member.content_sha256,
                )
            )
    targets.append(
        ProjectMutationArtifactTarget.create(
            target_kind="warpy_transform",
            resolution_kind="entry_member_from_current_project_snapshot",
            owner_root_uri=snapshot.project_uri,
            relative_locator_or_metadata_key=(snapshot.transform.semantic_locator),
            predecessor_sha256=snapshot.transform.content_sha256,
        )
    )
    targets.extend(target_plan.artifact_targets)
    for role in tuple(item.role for item in target_plan.role_scripts):
        targets.append(
            ProjectMutationArtifactTarget.create(
                target_kind="process_spawn_attempt",
                resolution_kind="authority_attestation_from_verified_evidence",
                owner_root_uri=project_provider.authority_root.as_uri(),
                relative_locator_or_metadata_key=f"spawn_attempt:{role}",
                predecessor_sha256=None,
            )
        )
    active_path = (
        project_provider.authority_root
        / "active-by-locator"
        / hashlib.sha256(snapshot.project_uri.encode("utf-8")).hexdigest()
        / "active.json"
    )
    control_specs = (
        (
            "artifact_resolution",
            "authority_attestation_from_verified_evidence",
            project_provider.authority_root.as_uri(),
            "planned",
            None,
        ),
        (
            "artifact_resolution",
            "authority_attestation_from_verified_evidence",
            project_provider.authority_root.as_uri(),
            "final",
            None,
        ),
        (
            "active_lease_record",
            "exact_uri",
            active_path.parent.as_uri(),
            active_path.name,
            predecessor(active_path),
        ),
        (
            "batch_state_head",
            "exact_uri",
            state_layout.state.as_uri(),
            state_layout.state_head_path.name,
            predecessor(state_layout.state_head_path),
        ),
        (
            "terminal_attempt",
            "authority_attestation_from_verified_evidence",
            state_layout.attempts.as_uri(),
            "terminal",
            None,
        ),
        (
            "registration_project_material",
            "authority_attestation_from_verified_evidence",
            project_provider.authority_root.as_uri(),
            f"registration-material/{target_plan.registration_exchange_id}.json",
            None,
        ),
    )
    for kind, resolution, owner, locator, predecessor in control_specs:
        targets.append(
            ProjectMutationArtifactTarget.create(
                target_kind=kind,
                resolution_kind=resolution,
                owner_root_uri=owner,
                relative_locator_or_metadata_key=locator,
                predecessor_sha256=predecessor,
            )
        )
    return tuple(targets)


def _ordered_roles(
    attempt_kind: str,
) -> tuple[RegistrationRole, ...]:
    if attempt_kind in {
        "initial_registration_import",
        "import_warpy_transform",
        "review_registration",
    }:
        return ("registration_import",)
    if attempt_kind == "accept_registration_and_transfer":
        return ("registration_import", "transfer_qc")
    raise BatchRegistrationSessionError("registration preflight attempt kind is unsupported")


def _validate_attempt_predecessor_shape(
    *,
    route: RegistrationExecutionRoute,
    attempt_kind: str,
    interaction_response_sha256: str | None,
    predecessor_terminal_record_sha256: str | None,
    predecessor_transform_snapshot: CurrentRegistrationProjectSnapshot | None,
    registration_predecessor_sha256: str | None,
    review_return: bool = False,
) -> str | None:
    authorization = route.context.authorization
    if (
        authorization.interaction_response_sha256 != interaction_response_sha256
        or authorization.interaction_predecessor_terminal_record_sha256
        != predecessor_terminal_record_sha256
    ):
        raise BatchRegistrationSessionError(
            "registration interaction predecessor differs from current authorization"
        )
    if attempt_kind == "initial_registration_import":
        if any(
            value is not None
            for value in (
                interaction_response_sha256,
                predecessor_terminal_record_sha256,
                predecessor_transform_snapshot,
                registration_predecessor_sha256,
            )
        ):
            raise BatchRegistrationSessionError(
                "initial registration import cannot claim a predecessor"
            )
        return None
    _sha(interaction_response_sha256, "interaction_response_sha256")
    _sha(
        predecessor_terminal_record_sha256,
        "predecessor_terminal_record_sha256",
    )
    if attempt_kind == "import_warpy_transform":
        if (
            type(predecessor_transform_snapshot) is not CurrentRegistrationProjectSnapshot
            or (predecessor_transform_snapshot.transform.presence != "absent" and not review_return)
            or registration_predecessor_sha256 is not None
        ):
            raise BatchRegistrationSessionError("Warpy import retry predecessor shape is invalid")
        # This snapshot belongs to the retained, validated interaction. Current
        # observation is checked after provider resolution, then joined to it
        # by _snapshot_transition_is_exact; rereading it as fresh rejects a
        # harmless later display-only project save.
        return predecessor_transform_snapshot.snapshot_resolution_sha256
    if (
        attempt_kind not in {"review_registration", "accept_registration_and_transfer"}
        or predecessor_transform_snapshot is not None
    ):
        raise BatchRegistrationSessionError("registration acceptance predecessors are invalid")
    _sha(registration_predecessor_sha256, "registration_predecessor_sha256")
    return None


def _validate_current_snapshot(
    route: RegistrationExecutionRoute,
    snapshot: CurrentRegistrationProjectSnapshot,
) -> None:
    current = route.current_execution
    head = route.state_head
    catalogue = route.catalogue
    primary = route.primary_entry
    moving = route.moving_entry
    if any(value is None for value in (current, head, catalogue, primary, moving)):
        raise BatchRegistrationSessionError("Warpy registration route lacks current owner facts")
    assert current is not None
    assert head is not None
    assert catalogue is not None
    assert primary is not None
    assert moving is not None
    fixed_observation = snapshot.fixed_entry
    moving_observation = snapshot.moving_entry
    # Native entry and batch catalogue projections describe different records.
    # Join their shared identity fields; each owner authenticates its own digest.
    if (
        snapshot.project_uri != catalogue.project_uri
        or snapshot.project_identity_sha256 != head.project_identity_sha256
        or snapshot.project_authority_head_sha256 != head.project_authority_head_sha256
        or not same_project_snapshot(
            Path(head.project_path), head.project_qpproj_snapshot_sha256,
            snapshot.qpproj_sha256,
        )
        or not same_project_snapshot(
            Path(head.project_path), catalogue.qpproj_snapshot_sha256,
            head.project_qpproj_snapshot_sha256,
        )
        or catalogue.catalogue_sha256 != head.project_catalogue_sha256
        or fixed_observation.project_entry_id != primary.project_entry_id
        or fixed_observation.source_uri != primary.canonical_uri
        or fixed_observation.source_image_sha256 != primary.image_sha256
        or fixed_observation.image_role != "primary_mif"
        or moving_observation.project_entry_id != moving.project_entry_id
        or moving_observation.source_uri != moving.canonical_uri
        or moving_observation.source_image_sha256 != moving.image_sha256
        or moving_observation.image_role != "moving_reference"
        or primary.paired_entry_id != moving.project_entry_id
        or moving.paired_entry_id != primary.project_entry_id
        or primary.project_entry_id == moving.project_entry_id
        or current.project_entry_ids != (primary.project_entry_id,)
    ):
        raise BatchRegistrationSessionError(
            "registration snapshot differs from the current reciprocal pair"
        )


def _settle_unused_registration_preparation(
    *,
    state_store: BatchStateStore,
    catalogue_store: BatchProjectCatalogueStore,
    workspace_lease: object,
    project_provider: ProjectMutationLeaseProvider,
    resolver: CurrentRegistrationProjectSnapshotResolver,
    runner: QuPathRunner,
    project: ProjectLayout,
) -> RegistrationProjectPreflight | None:
    """Settle unused preparation or reopen the original launched acquisition."""
    snapshot = project_provider.authenticate_current_registration_project_snapshot(resolver)
    candidates = project_provider._unresolved_active_records_for_project(
        project_uri=snapshot.project_uri, project_identity_sha256=snapshot.project_identity_sha256)
    if not candidates:
        return
    if len(candidates) != 1 or candidates[0][1].operation != "registration_project_write":
        raise BatchRegistrationSessionError("registration preparation recovery owner is ambiguous")
    active = candidates[0][1]
    acquisition = state_store.reopen_project_mutation_acquisition_record(
        active.acquisition_record_sha256, lease=workspace_lease)
    request, capability = catalogue_store._reopen_registration_project_request(
        active.mutation_request_sha256, workspace_lease=workspace_lease,
        state_store_issuer=state_store._project_durability_capability)
    if (request.snapshot_resolution_sha256 != snapshot.snapshot_resolution_sha256
            or request.state_head_sha256 != state_store.load_head().sha256):
        raise BatchRegistrationSessionError("unused registration preparation inputs changed")
    inventory = state_store.reopen_project_mutation_artifact_inventory(
        active.artifact_inventory_sha256, lease=workspace_lease)
    session = project_provider.issue_acquisition_session(
        acquisition, request=capability, inventory=inventory,
        durability_authority=state_store.project_mutation_durability_authority(workspace_lease))
    retained_inventory = session._artifact_inventory_capability.inventory
    if (project_provider._persisted_runtime_spawn_attempts(active, retained_inventory)
            or project_provider._persisted_runtime_process_launch_receipts(active, retained_inventory)
            or project_provider._persisted_runtime_process_launch(active, retained_inventory) is not None):
        target_plan = runner.reopen_registration_runtime_target_plan(
            project,
            snapshot,
            RegistrationRuntimeTargetPlanSeed.create(
                registration_exchange_id=request.registration_exchange_id,
                snapshot_resolution_sha256=request.snapshot_resolution_sha256,
                ordered_roles=request.ordered_roles,
            ),
            inventory=retained_inventory,
            expected_target_plan_sha256=request.runtime_target_plan_sha256,
        )
        return RegistrationProjectPreflight._issue(
            disposition="acquisition_ready",
            snapshot_resolver=resolver,
            snapshot=snapshot,
            outer_request=request,
            runtime_target_plan=target_plan,
            acquisition_session=session,
            recovery_record_sha256=active.lease_record_sha256,
        )
    with project_provider.recover(session, stale_active_record_sha256=active.lease_record_sha256) as lease:
        project_provider.abort_unlaunched_registration(lease, resolver=resolver)


def prepare_registration_project_preflight(
    route: RegistrationExecutionRoute,
    *,
    state_store: BatchStateStore,
    catalogue_store: BatchProjectCatalogueStore,
    workspace_lease: object,
    project_provider: ProjectMutationLeaseProvider,
    runner: QuPathRunner,
    project: ProjectLayout,
    attempt_kind: str,
    interaction_response_sha256: str | None = None,
    predecessor_terminal_record_sha256: str | None = None,
    predecessor_transform_snapshot: CurrentRegistrationProjectSnapshot | None = None,
    registration_predecessor_sha256: str | None = None,
) -> RegistrationProjectPreflight:
    """Resolve current Warpy material and close acquisition authority once."""

    authenticated = authenticate_registration_execution_route(route)
    if (
        authenticated.disposition != "warpy_r2"
        or type(state_store) is not BatchStateStore
        or type(catalogue_store) is not BatchProjectCatalogueStore
        or type(project_provider) is not ProjectMutationLeaseProvider
        or getattr(state_store, "_project_mutation_provider", None) is not project_provider
        or type(runner) is not QuPathRunner
        or type(project) is not ProjectLayout
    ):
        raise BatchRegistrationSessionError(
            "registration preflight requires its exact composed owners"
        )
    roles = _ordered_roles(attempt_kind)
    node = state_store._load_plan_snapshot(route.current_execution.active_plan_sha256).node_by_id[route.context.node_id]
    review_return = "settled_review_revision" in node
    transform_predecessor_sha256 = _validate_attempt_predecessor_shape(
        route=route,
        attempt_kind=attempt_kind,
        interaction_response_sha256=interaction_response_sha256,
        predecessor_terminal_record_sha256=predecessor_terminal_record_sha256,
        predecessor_transform_snapshot=predecessor_transform_snapshot,
        registration_predecessor_sha256=registration_predecessor_sha256,
        review_return=review_return,
    )
    state_store._assert_lease(workspace_lease)
    state_store._authenticate_project_stage_catalogue_store(
        catalogue_store,
        workspace_lease,
    )
    state_store._runtime_binding_for_runner(runner)
    workspace_scope = state_store.workspace_storage_scope_sha256
    _sha(workspace_scope, "workspace_storage_scope_sha256")
    current = state_store.authenticate_current_execution_context(route.context)
    head = state_store.load_head()
    catalogue = catalogue_store.load_current()
    routed_current = route.current_execution
    routed_head = route.state_head
    routed_catalogue = route.catalogue
    if (
        routed_current is None
        or routed_head is None
        or routed_catalogue is None
        or current._seal_sha256 != routed_current._seal_sha256
        or current.execution_context_sha256 != routed_current.execution_context_sha256
        or head.sha256 != routed_head.sha256
        or head.active_plan_sha256 != routed_head.active_plan_sha256
        or catalogue is None
        or catalogue.catalogue_sha256 != routed_catalogue.catalogue_sha256
    ):
        raise BatchRegistrationSessionError("registration route is no longer current")
    primary = route.primary_entry
    moving = route.moving_entry
    tissue = route.tissue_result
    if primary is None or moving is None or tissue is None:
        raise BatchRegistrationSessionError(
            "registration route lacks its exact project/tissue inputs"
        )
    if project.project_file.absolute().as_uri() != catalogue.project_uri:
        raise BatchRegistrationSessionError(
            "registration project layout differs from the current catalogue"
        )
    resolver = project_provider.resolve_current_registration_project_snapshot(
        project.project_file,
        fixed_project_entry_id=primary.project_entry_id,
        moving_project_entry_id=moving.project_entry_id,
    )
    snapshot = project_provider.authenticate_current_registration_project_snapshot(resolver)
    _validate_current_snapshot(route, snapshot)
    if project.project_file.absolute().as_uri() != snapshot.project_uri:
        raise BatchRegistrationSessionError(
            "registration project layout differs from the provider snapshot"
        )
    if snapshot.transform.presence == "absent" or (review_return and attempt_kind == "initial_registration_import"):
        if attempt_kind != "initial_registration_import" or any(
            value is not None
            for value in (
                interaction_response_sha256,
                predecessor_terminal_record_sha256,
                predecessor_transform_snapshot,
                registration_predecessor_sha256,
            )
        ):
            raise BatchRegistrationSessionError(
                "only the initial alignment choice may open without an accepted transform response"
            )
        return RegistrationProjectPreflight._issue(
            disposition="transform_choice_required",
            snapshot_resolver=resolver,
            snapshot=snapshot,
            outer_request=None,
            runtime_target_plan=None,
            acquisition_session=None,
        )
    if snapshot.transform.presence != "present" or snapshot.transform.content_sha256 is None:
        raise BatchRegistrationSessionError("registration transform observation is unsupported")
    if attempt_kind == "import_warpy_transform" and (
        predecessor_transform_snapshot is None
        or not _snapshot_transition_is_exact(
            predecessor_transform_snapshot,
            snapshot,
        )
    ):
        raise BatchRegistrationSessionError(
            "The alignment image pair changed since its saved transform choice. Reopen alignment setup."
        )
    assert workspace_scope is not None
    assert current is not None
    assert head is not None
    assert catalogue is not None
    recovery = _settle_unused_registration_preparation(
        state_store=state_store,
        catalogue_store=catalogue_store,
        workspace_lease=workspace_lease,
        project_provider=project_provider,
        resolver=resolver,
        runner=runner,
        project=project,
    )
    if recovery is not None:
        if (
            recovery.outer_request.attempt_kind != attempt_kind
            or recovery.outer_request.attempt_authorization_sha256 != current.authorization_sha256
            or recovery.outer_request.ordered_roles != roles
        ):
            raise BatchRegistrationSessionError(
                "registration recovery differs from current attempt"
            )
        return recovery
    exchange_id = issue_registration_exchange_id(
        current_execution=current,
        attempt_authorization_sha256=current.authorization_sha256,
        workspace_storage_scope_sha256=workspace_scope,
        snapshot=snapshot,
        catalogue_sha256=catalogue.catalogue_sha256,
        ordered_roles=roles,
    )
    seed = RegistrationRuntimeTargetPlanSeed.create(
        registration_exchange_id=exchange_id,
        snapshot_resolution_sha256=snapshot.snapshot_resolution_sha256,
        ordered_roles=roles,
    )
    target_plan = runner.plan_registration_runtime_targets(
        project,
        snapshot,
        seed,
    )
    runner.authenticate_registration_runtime_target_plan(target_plan)
    request = BatchRegistrationProjectRequest.create(
        batch_id=head.batch_id,
        node_id=current.node_id,
        attempt_id=route.context.authorization.attempt_id,
        attempt_authorization_sha256=current.authorization_sha256,
        execution_context_sha256=current.execution_context_sha256,
        state_head_sha256=head.sha256,
        active_plan_sha256=head.active_plan_sha256,
        workspace_storage_scope_sha256=workspace_scope,
        attempt_kind=attempt_kind,
        intent="open_existing",
        project_uri=snapshot.project_uri,
        project_identity_sha256=snapshot.project_identity_sha256,
        project_authority_head_sha256=(snapshot.project_authority_head_sha256),
        project_qpproj_snapshot_sha256=snapshot.qpproj_sha256,
        project_catalogue_sha256=catalogue.catalogue_sha256,
        fixed_project_entry_id=primary.project_entry_id,
        fixed_entry_projection_sha256=primary.entry_projection_sha256,
        fixed_image_sha256=primary.image_sha256,
        moving_project_entry_id=moving.project_entry_id,
        moving_entry_projection_sha256=moving.entry_projection_sha256,
        moving_image_sha256=moving.image_sha256,
        moving_reference_modality=moving.reference_modality,
        analysis_frame_ref_sha256=route.frame_ref.artifact_ref_sha256,
        tissue_acceptance_result_ref_sha256=(route.tissue_ref.artifact_ref_sha256),
        tissue_generation_id=tissue.generation_id,
        tissue_generation_content_sha256=tissue.generation_content_sha256,
        tissue_section_inventory_sha256=tissue.section_inventory_sha256,
        tissue_section_ids_sha256=_digest(list(tissue.section_ids)),
        tissue_roi_ids_sha256=_digest(list(tissue.roi_ids)),
        tissue_path_object_ids_sha256=_digest(list(tissue.path_object_ids)),
        snapshot_resolution_sha256=snapshot.snapshot_resolution_sha256,
        transform_uri=snapshot.transform.transform_uri,
        transform_sha256=snapshot.transform.content_sha256,
        interaction_response_sha256=interaction_response_sha256,
        predecessor_terminal_record_sha256=(predecessor_terminal_record_sha256),
        predecessor_transform_snapshot_resolution_sha256=(transform_predecessor_sha256),
        registration_predecessor_sha256=registration_predecessor_sha256,
        ordered_roles=roles,
        registration_exchange_id=exchange_id,
        runtime_target_plan_sha256=target_plan.target_plan_sha256,
        authorizing_record_sha256=current.authorization_sha256,
    )
    targets = _registration_static_targets(
        snapshot=snapshot,
        target_plan=target_plan,
        project_provider=project_provider,
        state_store=state_store,
        project=project,
    )
    prepared_inventory = ProjectMutationArtifactInventory.create(
        mutation_request_sha256=request.request_sha256,
        mutation_authorization_sha256=request.authorizing_record_sha256,
        expected_project_identity_sha256=request.project_identity_sha256,
        static_targets=targets,
        dynamic_targets=(),
    )
    request_capability = state_store._publish_registration_project_request(
        catalogue_store,
        request,
        route.context,
        lease=workspace_lease,
    )
    inventory = state_store.publish_project_mutation_artifact_inventory(
        prepared_inventory,
        lease=workspace_lease,
    )
    acquisition = state_store._publish_registration_project_mutation_acquisition_record(
        catalogue_store,
        route.context,
        request=request_capability,
        inventory=inventory,
        snapshot_resolver=resolver,
        target_plan=target_plan,
        project_provider=project_provider,
        runner=runner,
        project=project,
        lease=workspace_lease,
    )
    durability = state_store.project_mutation_durability_authority(workspace_lease)
    session = project_provider.issue_acquisition_session(
        acquisition,
        request=request_capability,
        inventory=inventory,
        durability_authority=durability,
    )
    return RegistrationProjectPreflight._issue(
        disposition="acquisition_ready",
        snapshot_resolver=resolver,
        snapshot=snapshot,
        outer_request=request,
        runtime_target_plan=target_plan,
        acquisition_session=session,
    )


def issue_registration_exchange_id(
    *,
    current_execution: BatchCurrentExecutionAuthority,
    attempt_authorization_sha256: str,
    workspace_storage_scope_sha256: str,
    snapshot: CurrentRegistrationProjectSnapshot,
    catalogue_sha256: str,
    ordered_roles: tuple[str, ...],
) -> str:
    """Derive the digest-cycle-free identity shared by state and runner."""

    if (
        type(current_execution) is not BatchCurrentExecutionAuthority
        or current_execution.node_kind != "registration"
        or current_execution.node_scope != "shared_project"
        or len(current_execution.participant_ids) != 1
        or len(current_execution.project_entry_ids) != 1
        or current_execution.authorization_sha256 != attempt_authorization_sha256
        or type(snapshot) is not CurrentRegistrationProjectSnapshot
        or snapshot.transform.presence != "present"
        or snapshot.transform.content_sha256 is None
        or current_execution.project_entry_ids != (snapshot.fixed_entry.project_entry_id,)
    ):
        raise BatchRegistrationSessionError(
            "registration exchange inputs are not one current Warpy execution"
        )
    roles: tuple[RegistrationRole, ...]
    if ordered_roles == ("registration_import",):
        roles = ("registration_import",)
    elif ordered_roles == ("registration_import", "transfer_qc"):
        roles = ("registration_import", "transfer_qc")
    else:
        raise BatchRegistrationSessionError(
            "registration exchange roles are unsupported or unordered"
        )
    preimage = {
        "schema_name": "mif-registration-exchange-identity",
        "schema_version": 1,
        "node_id": current_execution.node_id,
        "attempt_authorization_sha256": _sha(
            attempt_authorization_sha256,
            "attempt_authorization_sha256",
        ),
        "execution_context_sha256": current_execution.execution_context_sha256,
        "current_execution_seal_sha256": current_execution._seal_sha256,
        "state_head_sha256": current_execution.state_head_sha256,
        "active_plan_sha256": current_execution.active_plan_sha256,
        "workspace_storage_scope_sha256": _sha(
            workspace_storage_scope_sha256,
            "workspace_storage_scope_sha256",
        ),
        "project_identity_sha256": snapshot.project_identity_sha256,
        "project_authority_head_sha256": (snapshot.project_authority_head_sha256),
        "project_qpproj_snapshot_sha256": snapshot.qpproj_sha256,
        "project_catalogue_sha256": _sha(
            catalogue_sha256,
            "catalogue_sha256",
        ),
        "fixed_entry_projection_sha256": (snapshot.fixed_entry.entry_projection_sha256),
        "moving_entry_projection_sha256": (snapshot.moving_entry.entry_projection_sha256),
        "snapshot_resolution_sha256": snapshot.snapshot_resolution_sha256,
        "transform_sha256": snapshot.transform.content_sha256,
        "ordered_roles": list(roles),
    }
    return hashlib.sha256(canonical_json_bytes(preimage)).hexdigest()


__all__ = [
    "BatchRegistrationSessionError",
    "RegistrationProjectPreflight",
    "build_registration_transform_interaction",
    "issue_registration_exchange_id",
    "prepare_registration_project_preflight",
]
