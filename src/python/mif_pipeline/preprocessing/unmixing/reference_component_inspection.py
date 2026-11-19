"""Read checkpoint-owned accepted reference evidence without changing the workflow."""


def inspect_accepted_control_preparation(service, run_id, checkpoint_token, *,
                                         input_id, expected_preparation_sha256):
    from .reference_service_control_preparation import (
        accepted_preparation, control_source_row, project_control_preparation)

    snapshot = service.resume(run_id)
    current = snapshot.checkpoint
    if current.content_sha256 != checkpoint_token:
        raise ValueError('The reference review changed. Reopen its preparation overview.')
    if not any(item.response.option_value == 'accept_reference_control_files_with_source_preparation'
               for item in current.active_decisions):
        raise ValueError('This control does not have an accepted preparation.')
    start = snapshot.start_authority_sha256
    binding = service._one_artifact(run_id, start, 'reference_control_files', 'reference-control-files').record
    identities = ((binding.shared_unstained_input_id,) if binding.shared_unstained_input_id is not None
                  else binding.negative_control_input_ids)
    canonical = service._canonical_inventory(run_id, start)
    record = next((item for item in canonical if item.input_id == input_id), None)
    if input_id not in identities or record is None:
        raise ValueError('This control does not have an accepted preparation.')
    source = accepted_preparation(service, current, start, canonical_inputs=canonical, input_id=input_id)
    if source is None:
        raise ValueError('This control does not have an accepted preparation.')
    if source.content_sha256 != expected_preparation_sha256:
        raise ValueError('The accepted control preparation changed. Reopen its preparation overview.')
    dependents = (current.fluorophores if binding.shared_unstained_input_id is not None else
                  (current.fluorophores[identities.index(input_id)],))
    result = project_control_preparation(source,
        control_source_row(binding, record, source, dependents, accepted=True))
    if service.resume(run_id).checkpoint.content_sha256 != checkpoint_token:
        raise ValueError('The reference review changed. Reopen its preparation overview.')
    return result


def inspect_accepted_component(service, run_id, checkpoint_token, component):
    from .reference_service import _reference_wavelength_fields

    snapshot = service.resume(run_id)
    current = snapshot.checkpoint
    if current.content_sha256 != checkpoint_token:
        raise ValueError('The reference review changed. Reopen its component overview.')
    slots = [slot for slot in current.fluorophore_artifacts
             if slot.fluorophore == component and slot.accepted_spectrum is not None]
    if len(slots) != 1:
        raise ValueError('This component does not have an accepted spectrum.')
    spectrum = service._artifacts.load_pointer(run_id=run_id,
        start_authority_sha256=snapshot.start_authority_sha256,
        kind='accepted_spectrum', subject_id=component, pointer=slots[0].accepted_spectrum).record
    target = service._require_canonical_target(run_id, snapshot.start_authority_sha256)
    if (spectrum.fluorophore_identity != component
            or current.measurement_ids != target.evidence.axis.measurement_ids
            or len(spectrum.values) != len(target.evidence.axis.parent_band_ids)):
        raise ValueError('The accepted spectrum does not match its component and measurement bands.')
    if service.resume(run_id).checkpoint.content_sha256 != checkpoint_token:
        raise ValueError('The reference review changed. Reopen its component overview.')
    return {'component': component, 'values': list(spectrum.values),
            'normalization_convention': spectrum.normalization_convention,
            'measurement_names': list(target.evidence.axis.parent_band_ids),
            **dict(_reference_wavelength_fields(target.evidence.axis))}
