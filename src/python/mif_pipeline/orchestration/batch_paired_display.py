"""Read-only paired-image display scope from current accepted registrations."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from .batch_registration import decode_committed_registration_result, reopen_accepted_warpy_record
from mif_pipeline.registration.backend import registration_record_sha256

# ==============================================================================
#                              Batch paired display
# ==============================================================================


def registered_pair_displays(store, snapshot):
    result = []
    seen = set()
    head = snapshot.head
    for authority in snapshot.committed_artifact_authorities:
        if authority.artifact_ref.artifact_kind != "registration_result":
            continue
        if (
            authority.state_head_sha256 != head.sha256
            or authority.active_plan_sha256 != head.active_plan_sha256
        ):
            raise ValueError("The paired-image display belongs to an earlier analysis state")
        projection = decode_committed_registration_result(authority)
        if projection.mode != "warpy":
            continue
        terminal = store.load_attempt(authority.terminal_record_sha256)
        authorization = store.load_authorization(terminal.authorization_sha256)
        record = reopen_accepted_warpy_record(store, authorization)
        if (
            registration_record_sha256(record)
            != projection.artifact_ref.owning_identity["accepted_warpy_record_sha256"]
            or record.acceptance_state != "accepted"
            or record.fixed_project_entry_id != projection.primary_project_entry_id
            or record.moving_project_entry_id != projection.moving_project_entry_id
            or record.fixed_image_sha256 != projection.primary_image_sha256
            or record.moving_image_sha256 != projection.moving_image_sha256
        ):
            raise ValueError("The paired-image review no longer matches the accepted registration")
        pair = (record.fixed_project_entry_id, record.moving_project_entry_id)
        if pair in seen:
            raise ValueError("More than one current registration matches this pair")
        seen.add(pair)
        result.append(
            {
                "fixed_entry_id": pair[0],
                "moving_entry_id": pair[1],
                "sample_id": projection.sample_id,
                "fixed_image_id": record.fixed_image_id,
                "fixed_image_sha256": record.fixed_image_sha256,
                "moving_image_id": record.moving_image_id,
                "moving_image_sha256": record.moving_image_sha256,
                "moving_modality": record.moving_modality,
                "fixed_physical_calibration": record.fixed_physical_calibration.to_mapping(),
                "fixed_image_uri": record.fixed_image_path.as_uri(),
                "moving_image_uri": record.moving_image_path.as_uri(),
                "transform_path": str(record.transform_file),
                "transform_sha256": record.transform_file_sha256,
                "registration_sha256": registration_record_sha256(record),
                "tissue_object_ids": list(record.tissue_path_object_ids),
            }
        )
    return tuple(result)
