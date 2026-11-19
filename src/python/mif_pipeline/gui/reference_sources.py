"""Project required reference sources without changing their binding authority."""
from dataclasses import replace


def reference_source_form(form):
    state = form.context.get("stage_result", {}).get("resume_context", {}).get("service_context", {}).get("operation_state", {})
    if state.get("checkpoint") != "reference_control_files":
        return form
    context = dict(form.context, reference_source_step=True)
    if context.get("selected_option") not in {
            'accept_reference_control_files', 'accept_reference_control_files_with_source_preparation'}:
        return replace(form, context=context)
    inventory = state.get("review_summary", {}).get("reference_file_choices", {})
    roles = {"positive_input_ids": ("positive_reference_image", "Single-stain images"),
             "negative_input_ids": ("negative_reference_image", "Negative controls"),
             "shared_unstained_input_id": ("unstained_reference_image", "Shared unstained control")}
    registration = {}
    for requirement in state.get("operation_requirements", ()):
        if requirement.get("operation_action") == "register_canonical_input":
            artifacts = requirement.get("artifact_requirements", ())
            if len(artifacts) == 1:
                artifact = artifacts[0]
                registration[(artifact["role"], artifact.get("subject_id"))] = requirement["option_value"]
    groups = []
    for field, names in context.get("reference_file_bindings", {}).items():
        role, title = roles[field]
        stains = (None,) if field == "shared_unstained_input_id" else inventory["fluorophores"]
        rows = tuple({"control": name, "label": stain or "Unstained tissue",
                      "option": registration.get((role, "shared" if field == "shared_unstained_input_id" else stain))}
                     for name, stain in zip(names, stains, strict=True))
        groups.append({"title": title, "rows": rows})
    if groups:
        context["reference_source_overview"] = tuple(groups)
    return replace(form, title="Add reference images",
        message="Add the required single-stain images and controls. Each file opens for review of its band names and imaging settings.",
        context=context)
