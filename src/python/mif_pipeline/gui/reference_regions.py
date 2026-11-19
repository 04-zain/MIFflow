"""Present native spectral-reference regions within the existing review form."""
from dataclasses import replace
from mif_pipeline.gui.interactions import ControlKind, FormControl, FormTable, FormTableRow

REGION_CHECKPOINTS = {"shared_control_preparation", "positive_reference_rois", "af_reference_rois",
                      "negative_reference_rois", "background_reference_rois"}


def reference_region_form(form, summary=None):
    """Attach saved region rows and exact selections without changing review authority."""
    owner = form.context.get("stage_result", {}).get("resume_context", {}).get("service_context", {})
    state = owner.get("operation_state", {})
    checkpoint = state.get("checkpoint")
    if form.context.get('selected_option') == 'review_control_sets':
        from .reference_control_preparation import reference_control_preparation_form
        return reference_control_preparation_form(form)
    if (checkpoint not in REGION_CHECKPOINTS
            or form.context.get("selected_option") in {"reselect_reference_rois", "abort",
                "retry_shared_control_preparation", "revise_shared_control", 'revise_component_preparation'}):
        return form
    if summary is not None and summary["checkpoint_token"] != state["checkpoint_token"]:
        raise ValueError("The reference-region result belongs to an earlier review.")
    if form.context.get('selected_option') in {'accept_shared_control_preparation', 'assign_shared_control_sets'}:
        from .reference_control_preparation import reference_control_preparation_form
        return reference_control_preparation_form(form)
    if form.context.get('selected_option') in {'accept_reference_regions', 'prepare_shared_control', 'prepare_stained_reference'}:
        return _unified_region_form(form, state, summary)
    context = dict(form.context)
    accepted = bool(summary and summary["accepted"])
    context.update(reference_regions_available=True, reference_regions_summary=summary,
                   reference_regions_checkpoint=checkpoint, reference_region_values={})
    if "reselect_reference_rois" in state.get("offered_actions", ()):
        context["back_option"] = "reselect_reference_rois"
        context["back_label"] = "Back: region editing"
    controls = []
    labels = {row["roi_id"]: f'{row["image_name"]} — {row["label"]} {row["number"]}'
              for row in summary["rows"]} if summary else {}
    # Mapping keys remain exact ROI IDs; only their displayed labels use the
    # already loaded filename, role and region number.
    pairing_fields = {
        f"reference_mapping_{field_index}_{index}_value": key
        for requirement in state.get("operation_requirements", ())
        if requirement["option_value"] == context.get("selected_option")
        for field_index, field in enumerate(requirement.get("form_fields", ()))
        if field["name"] == "pairings"
        for index, key in enumerate(field.get("mapping_key_choices", ()))
    }
    control_sets_enabled = any(field.get('value_kind') == 'identifier_multimapping'
        for requirement in state.get('operation_requirements', ())
        if requirement['option_value'] == context.get('selected_option')
        for field in requirement.get('form_fields', ()) if field['name'] == 'pairings')
    pairing_labels = {name: f"Control for {labels[key]}" for name, key in pairing_fields.items() if key in labels}
    choice_labels = dict(context.get("choice_labels", {}))
    selected = tuple(summary["selected_roi_ids"]) if summary else ()
    for control in form.controls:
        if control.name in pairing_labels:
            control = replace(control, label=pairing_labels[control.name], help_text=None)
        if control.name == "roi_export_locator":
            continue
        if control.name == "selected_roi_ids":
            control = replace(control, label="Regions to use", choices=selected, value=selected,
                              help_text="Choose the saved regions for this step.")
        elif accepted and control.name.startswith("reference_mapping_") and control.name.endswith("_value"):
            control = replace(control, kind=ControlKind.OBJECT_SEQUENCE if control_sets_enabled else ControlKind.CHOICE,
                              choices=selected, value=selected if control_sets_enabled else selected[0] if len(selected) == 1 else None)
        if control.name == "selected_roi_ids" or control.name.startswith("reference_mapping_"):
            choice_labels[control.name] = labels
        controls.append(control)
    choice_labels["option_value"] = {
        **choice_labels.get("option_value", {}),
        **{f"{verb}_{name}_reference_rois": f"{action} {label} regions"
           for name, label in (("positive", "stained"), ("af", "autofluorescence"),
                               ("negative", "negative-control"), ("background", "background"))
           for verb, action in (("accept", "Use selected"), ("retry", "Repeat selection of"))},
    }
    context["choice_labels"] = choice_labels
    context["plain_text_sequences"] = tuple(name for name in context.get("plain_text_sequences", ())
                                               if name != "selected_roi_ids")
    if not summary:
        context["locked_sequences"] = ("selected_roi_ids",)
    else:
        context.pop("locked_sequences", None)
    if accepted:
        context["reference_region_values"] = {"roi_export_locator": summary["locator"]}
        context["plain_text_sequences"] = tuple(name for name in context.get("plain_text_sequences", ())
                                                   if name != "selected_roi_ids")
    tables = tuple(table for table in form.tables if table.table_id != "native_reference_regions")
    if summary and summary["rows"]:
        tables += (FormTable("native_reference_regions", ("Image", "Role", "Region", "Area (µm²)", "Review"),
            tuple(FormTableRow(row["roi_id"], (row["image_name"], row["label"], row["number"],
                f'{row["area_um2"]:.1f}', row["state"])) for row in summary["rows"]),
            title="Saved reference regions"),)
    control_instruction = (
        "Negative reference for the matched per-stain control"
        if state.get("review_summary", {}).get("control_strategy") == "per_stain_negative_controls"
        else "Autofluorescence control for unstained tissue"
    )
    context["reference_region_workspace"] = {
        "checkpoint": checkpoint, "accepted": accepted,
        "rows": tuple(summary["rows"]) if summary else (), "selectable_ids": selected,
        "pairing_fields": pairing_fields,
        "control_sets_enabled": control_sets_enabled,
        "has_selection": any(control.name == "selected_roi_ids" for control in controls),
        "drawing_guide": "Assign Positive reference to stained tissue, " + control_instruction
            + ", and Image background to background regions in each image. "
            "Only these classes are included. Save each image and close QuPath before loading the regions.",
    }
    message = {
        "positive_reference_rois": "Select the stained regions to sample. Draw or change outlines in QuPath, then load and accept the saved regions.",
        "af_reference_rois": "Combine compatible autofluorescence regions into a control set, then choose the set used for each stained region. Review its spectrum before extracting the fluorophore.",
        "negative_reference_rois": "Match each stained region to a region in its negative-control image with comparable tissue. The matched controls supply the background spectra used during extraction.",
        "background_reference_rois": "Select background regions for the stained and control images.",
    }[checkpoint]
    actions = tuple(form.context["stage_result"]["allowed_actions"]) if accepted else ()
    return replace(form, message=message, controls=tuple(controls), tables=tables,
                   allowed_actions=actions, context=context)


def _unified_region_form(form, state, summary):
    """Bind source annotations or a retained combined draft to its exact export."""
    context = dict(form.context)
    accepted = bool(summary and summary['accepted'])
    rows = tuple(summary['rows']) if summary else ()
    strategy = state.get('review_summary', {}).get('control_strategy')
    preparation = {'prepare_shared_control': 'control', 'prepare_stained_reference': 'stained'}.get(
        context.get('selected_option'))
    progress = state.get('review_summary', {}).get('reference_preparation_progress', {})
    control_source = (state.get('review_summary', {}).get('control_preparation') or
        progress.get('control_source') or progress.get('shared_control') or {})
    control_role = (control_source.get('control_roi_role', 'af') if preparation else
                    'negative' if strategy == 'per_stain_negative_controls' else 'af')
    # Source names describe the project opened by the global native action.
    # Prefer accepted input metadata; older retained reviews can recover the
    # same identity from loaded region rows without opening source pixels.
    project_images = ((summary or {}).get('project_images')
                      or state.get('review_summary', {}).get('reference_region_images', ()))
    if not project_images:
        sources = {}
        for row in rows:
            if row['role'] not in {'positive', control_role}:
                continue
            source = row.get('source', row['image_name'])
            sources.setdefault(source, {'input_id': source, 'filename': row['image_name'], 'role': row['role']})
        project_images = tuple(sources.values())
    ids = lambda role: [row['roi_id'] for row in rows if row['role'] == role]
    positives, controls, backgrounds = ids('positive'), ids(control_role), ids('background')
    if preparation:
        roles = {'single_component_reference', 'positive'} if preparation == 'stained' else {
            'unstained_control', 'af', 'negative_control', 'single_stain_negative_control'}
        project_images = tuple(source for source in project_images if source['role'] in roles)
        if preparation == 'control' and control_source.get('input_id'):
            project_images = tuple(source for source in project_images if source['input_id'] == control_source['input_id'])
    selection = {'positive_roi_ids': positives, 'background_roi_ids': backgrounds,
        'pairings': {identity: ([] if control_role == 'negative' and len(controls) != 1 else controls[:])
                     for identity in positives},
        'control_sets': [controls[:]] if controls else [[]]}
    if preparation:
        selection['pairings'] = {}
        if preparation == 'stained':
            selection['control_sets'] = []
    if summary and summary.get('saved_selection'):
        selection.update(summary['saved_selection'])
    settings = summary.get('saved_settings', {}) if summary else {}
    if preparation:
        constants = dict(context.get('reference_settings_constants', {}))
        constants.update({key: settings[key] for key in constants if key in settings})
        context['reference_settings_constants'] = constants
    projected = []
    for control in form.controls:
        if control.name == 'roi_export_locator':
            continue
        if control.name == 'reference_region_selection':
            control = replace(control, kind=ControlKind.JSON_OBJECT, value=selection, help_text=None)
        path = context.get('reference_settings_paths', {}).get(control.name)
        if path and path[0] in settings:
            value = settings
            for key in path:
                value = value[key]
            control = replace(control, value=value)
        projected.append(control)
    context.update(reference_regions_available=True, reference_regions_summary=summary,
        reference_regions_checkpoint=state['checkpoint'],
        reference_region_values={'roi_export_locator': summary['locator']} if accepted else {},
        reference_region_workspace={'unified': True, 'checkpoint': state['checkpoint'],
            'accepted': accepted, 'rows': rows, 'component': state.get('fluorophore'),
            'preparation_role': preparation,
            'control_roi_role': control_role,
            'source_role': control_source.get('source_role'),
            'source_link_id': control_source.get('source_link_id'),
            'project_images': tuple(project_images),
            'control_strategy': strategy, 'positive_ids': positives, 'control_ids': controls,
            'background_ids': backgrounds, 'saved_selection': selection})
    context.pop('back_option', None)
    context.pop('back_label', None)
    title = ('AF preparation' if preparation == 'control' else
        ('Single-stain preparation' if preparation == 'stained' else 'Reference annotations')
        + (f" · {state['fluorophore']}" if state.get('fluorophore') else ''))
    return replace(form, title=title, message='', controls=tuple(projected), context=context,
        allowed_actions=tuple(context['stage_result']['allowed_actions']) if accepted else ())


def reference_file_form(form):
    """Replace opaque file IDs with choices filtered by the saved role and stain."""
    state = form.context.get("stage_result", {}).get("resume_context", {}).get("service_context", {}).get("operation_state", {})
    inventory = state.get("review_summary", {}).get("reference_file_choices")
    if state.get("checkpoint") != "reference_control_files" or not inventory:
        return form
    fields = {"positive_input_ids": ("single_component_reference", "Single-stain image"),
              "negative_input_ids": ("single_stain_negative_control", "Negative control"),
              "shared_unstained_input_id": ("unstained_control", "Unstained control")}
    context = dict(form.context)
    labels = dict(context.get("choice_labels", {}))
    bindings = {}
    controls = []
    for control in form.controls:
        if control.name not in fields:
            controls.append(control)
            continue
        role, title = fields[control.name]
        stains = (None,) if role == "unstained_control" else inventory["fluorophores"]
        names = []
        for index, stain in enumerate(stains):
            rows = tuple(row for row in inventory["inputs"] if row["role"] == role
                         and (stain is None or row["fluorophore"] == stain))
            name = f"reference_file_{control.name}_{index}"
            ids = tuple(row["input_id"] for row in rows)
            controls.append(FormControl(name, title if stain is None else f"{title}: {stain}",
                ControlKind.CHOICE, choices=ids, value=ids[0] if len(ids) == 1 else None,
                help_text=None if ids
                          else "Upload and review the matching reference image before continuing."))
            labels[name] = {row["input_id"]: row["filename"] for row in rows}
            names.append(name)
        bindings[control.name] = tuple(names)
    context["reference_file_bindings"] = bindings
    context["choice_labels"] = labels
    context["plain_text_sequences"] = tuple(name for name in context.get("plain_text_sequences", ())
                                             if name not in bindings)
    return replace(form, controls=tuple(controls), context=context,
                   message="Choose the uploaded single-stain images and their matching controls."
                           if bindings else form.message)


def restore_reference_file_values(values, bindings):
    """Return selected filename choices in the existing ordered input-ID fields."""
    for field, names in bindings.items():
        selected = tuple(values.pop(name) for name in names)
        values[field] = selected[0] if field == "shared_unstained_input_id" else selected
