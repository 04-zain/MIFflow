"""Present accepted tissue choices without reopening or authorizing a review."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from mif_pipeline.pipeline_core.config import TissueDetectionParameters
from mif_pipeline.pipeline_core import StageStatus
from .batch_artifacts import BatchArtifactError
from .batch_tissue_setup_artifacts import decode_committed_tissue_setup_result
from .batch_tissue_resolution_artifacts import decode_committed_tissue_resolution_result

TISSUE_SETTINGS_KINDS = frozenset({"tissue_setup", "tissue_resolution", "tissue_candidate"})


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class SavedTissueSettings:
    """Passive controls and identities from the current accepted tissue artifacts.

    parameters describes the detector setup, including its requested resolution
    mode. resolution_selection separately records the native level actually chosen
    afterward. Neither record authorizes submission. The GUI must obtain a fresh
    pending request before binding a changed draft to normal interaction submission.
    """

    parameters: TissueDetectionParameters
    classifier_sha256: str
    channels: tuple[Mapping[str, object], ...]
    native_pyramid_inventory: tuple[Mapping[str, object], ...]
    compatible_native_resolution_indices: tuple[int, ...]
    resolution_selection: Mapping[str, object] | None
    setup_node_id: str
    resolution_node_id: str | None
    setup_artifact_ref_sha256: str
    resolution_artifact_ref_sha256: str | None
    project_path: str
    project_entry_id: str

    def to_mapping(self):
        """Serialize presentation values for existing local/remote stage options."""
        return {name: self.parameters.to_mapping() if name == "parameters" else _plain(getattr(self, name))
                for name in self.__dataclass_fields__}

    @classmethod
    def from_mapping(cls, value):
        """Restore display data, not scientific or interaction authority."""
        fields = dict(value)
        fields["parameters"] = TissueDetectionParameters.from_mapping(fields["parameters"], field="saved tissue settings")
        for name in ("channels", "native_pyramid_inventory", "compatible_native_resolution_indices"):
            fields[name] = tuple(fields[name])
        return cls(**fields)


def _setup_authority(snapshot, node, *, required=True):
    """Find the selected tissue task's current accepted upstream setup once."""
    nodes = {item.node_id: item for item in snapshot.plan.nodes}
    pending, ancestors = [node.node_id], set()
    while pending:
        key = pending.pop()
        if key in ancestors:
            continue
        ancestors.add(key)
        pending.extend(nodes[key].dependency_ids)
    candidates = [item for item in snapshot.committed_artifact_authorities
                  if item.artifact_ref.artifact_kind == "tissue_setup_result"
                  and item.artifact_ref.producer_node_id in ancestors
                  and item.artifact_ref.scope.sample_id in node.participant_ids
                  and item.artifact_ref.scope.validation_domain_id == node.validation_domain_id]
    if not candidates and not required:
        return None
    if len(candidates) != 1:
        raise BatchArtifactError("This tissue step has no unique accepted detector setup to display.")
    return candidates[0]


def saved_tissue_view_target(snapshot, node):
    """Resolve Back from a pending later review to its completed setup for display.

    Only public read-only stage-options queries use this resolver. Revision preview
    and apply retain their existing settled-node status requirements; callers must
    select the returned completed owner explicitly before requesting a change.
    """
    if (node.kind not in {"tissue_resolution", "tissue_candidate"}
            or snapshot.head.node_status_by_id.get(node.node_id) is not StageStatus.NEEDS_INTERACTION):
        return node
    authority = _setup_authority(snapshot, node)
    target = next(item for item in snapshot.plan.nodes if item.node_id == authority.artifact_ref.producer_node_id)
    if snapshot.head.node_status_by_id.get(target.node_id) is not StageStatus.COMPLETED:
        raise BatchArtifactError("The saved detector setup is not a current completed step.")
    return target


def saved_tissue_settings(snapshot, node):
    """Decode accepted controls from the current setup and its chosen resolution.

    The execution snapshot supplies authenticated committed authorities. Match the
    chosen resolution to the exact setup reference and content digest; do not borrow
    a level from a different detector revision. Only retained metadata are read:
    this projection opens no image, launches no native process and acquires no lease.
    """
    authority = _setup_authority(snapshot, node, required=False)
    if authority is None:
        return None
    setup = decode_committed_tissue_setup_result(authority)
    setup_ref = setup.artifact_ref
    candidates = [item for item in snapshot.committed_artifact_authorities
                  if item.artifact_ref.artifact_kind == "tissue_resolution_result"
                  and item.artifact_ref.owning_identity.get("tissue_setup_result_ref_sha256") == setup_ref.artifact_ref_sha256]
    if len(candidates) > 1:
        raise BatchArtifactError("This detector setup has more than one current chosen resolution.")
    resolution = decode_committed_tissue_resolution_result(candidates[0]) if candidates else None
    if resolution is not None and (
            resolution.tissue_setup_result_sha256 != setup.tissue_setup_result_sha256
            or resolution.sample_id != setup.sample_id
            or resolution.validation_domain_id != setup.validation_domain_id
            or resolution.project_entry_id != setup.project_entry_id):
        raise BatchArtifactError("The saved tissue resolution belongs to a different detector setup.")
    parameters = setup.tissue_detection.to_mapping()
    classifier_sha256 = parameters.pop("classifier_sha256")
    return SavedTissueSettings(
        parameters=TissueDetectionParameters.from_mapping(parameters, field="tissue_detection"),
        classifier_sha256=classifier_sha256,
        channels=tuple(_plain(item) for item in setup.tissue_setup_request["channels"]),
        native_pyramid_inventory=tuple(item.to_mapping() for item in setup.native_pyramid_inventory),
        compatible_native_resolution_indices=tuple(setup.native_observation["compatible_native_resolution_indices"]),
        resolution_selection=None if resolution is None else resolution.selection.to_mapping(),
        setup_node_id=setup_ref.producer_node_id,
        resolution_node_id=None if resolution is None else resolution.artifact_ref.producer_node_id,
        setup_artifact_ref_sha256=setup_ref.artifact_ref_sha256,
        resolution_artifact_ref_sha256=None if resolution is None else resolution.artifact_ref.artifact_ref_sha256,
        project_path=snapshot.head.project_path, project_entry_id=setup.project_entry_id,
    )
