"""Associate saved marker-call figures with the selected accepted cell export."""
from itertools import combinations
from urllib.parse import quote

from .batch_service_models import BatchSavedStageFigure


def _accepted_gates(authority, snapshot):
    from .batch_phenotyping.marker_application_execution import decode_committed_marker_gate
    from .batch_phenotyping.semantic_records import normalize_committed_phenotype_application
    from .batch_phenotyping.marker_combinations import decode_marker_combination

    identity = authority.artifact_ref.owning_identity
    if identity["selected_phenotype_view"] != "marker_rules":
        return {}
    current = {item.artifact_ref.artifact_ref_sha256: item for item in snapshot.committed_artifact_authorities}
    gates, scopes = {}, {}
    for reference in identity["phenotype_source_ref_sha256s"]:
        application = current.get(reference)
        if application is None or application.artifact_ref.artifact_kind != "marker_phenotype_result":
            raise ValueError("The saved marker application is no longer current.")
        normalize_committed_phenotype_application((application,))
        linked = [current[ref] for ref in application.artifact_ref.owning_identity["source_ref_sha256s"]
                  if ref in current and current[ref].artifact_ref.artifact_kind == "marker_gate"]
        if len(linked) != 1:
            raise ValueError("The accepted marker-gate association is unavailable.")
        gate = linked[0]
        key = gate.artifact_ref.artifact_ref_sha256
        if key not in gates:
            gates[key] = decode_committed_marker_gate(gate)
        application_identity = application.artifact_ref.owning_identity
        definition_refs = [current[ref] for ref in application_identity["source_ref_sha256s"]
                           if ref in current and current[ref].artifact_ref.artifact_kind == "marker_combination"]
        if len(definition_refs) == 1:
            definition = decode_marker_combination(definition_refs[0], gates[key])
        else:
            raise ValueError("The accepted phenotype combination selection is unavailable.")
        scopes[application_identity['slide_id'], application_identity['section_id']] = (gates[key], definition)
    return scopes


def _marker_ids(gates):
    values = tuple(gate.canonical_marker_ids for gate, _definition in gates.values())
    if not values:
        return ()
    if any(markers != values[0] for markers in values):
        raise ValueError("The saved sections do not share the same accepted marker selection.")
    return values[0]


def coexpression_choices(authority, snapshot):
    """Offer bounded explicit marker sets without reading cell rows on navigation."""
    gates = _accepted_gates(authority, snapshot)
    calls, message = _choices(_marker_ids(gates))
    rules = _rule_choices(_rule_groups(gates))
    return (*rules, *calls), message


def _rule_groups(gates):
    from .batch_phenotyping.marker_application_execution import _composites
    from mif_pipeline.cell_analysis.marker_rule_plots import group_rule_definitions

    return group_rule_definitions({scope: _composites(definition, gate.canonical_marker_ids)[:2]
                                   for scope, (gate, definition) in gates.items()})


def _rule_choices(groups):
    from mif_pipeline.cell_analysis.result_plots import COMPOSITION_PAGE_SIZE

    count = sum(len(group['rules']) for group in groups)
    return tuple(BatchSavedStageFigure(
        f"coexpression/rules/{page}", "Phenotype rule assignments" +
        (f" · Rules {page * COMPOSITION_PAGE_SIZE + 1}–{min(count, (page + 1) * COMPOSITION_PAGE_SIZE)}"
         if count > COMPOSITION_PAGE_SIZE else ""),
        "Saved rule matches, assigned cells and overlap with earlier rules. Unknown required markers and cells outside the selected population are reported separately.",
        data_formats=("csv",)) for page in range((count + COMPOSITION_PAGE_SIZE - 1) // COMPOSITION_PAGE_SIZE))


def _choices(markers):
    from mif_pipeline.cell_analysis.coexpression_plots import MAX_MARKERS

    if not markers:
        return (), "Co-expression requires saved marker-rule calls for this cell result."
    if len(markers) < 2:
        return (), "Co-expression requires at least two accepted markers."
    if len(markers) > MAX_MARKERS:
        return (BatchSavedStageFigure(
            'coexpression/selected', 'Co-expression · Selected markers',
            'Compare saved positive and negative calls for selected markers. Unknown calls are counted separately.',
            settings={'markers': list(markers)}, data_formats=('csv',)),), ''
    selections = [("coexpression/all", markers)]
    if len(markers) > 2:
        selections.extend(("coexpression/pair/" + "/".join(quote(marker, safe="") for marker in pair), pair)
                          for pair in combinations(markers, 2))
    choices = tuple(BatchSavedStageFigure(
        key, "Co-expression · " + (f"All {len(markers)} markers" if key.endswith("/all") else " + ".join(selected)),
        "Saved calls for " + ", ".join(selected) + ". Percentages use cells with complete selected-marker calls; unknown and not-phenotyped cells are reported separately. Up to 20 combinations are shown individually, with remaining counts grouped explicitly.",
        data_formats=("csv",))
        for key, selected in selections)
    return choices, "Co-expression uses accepted per-cell marker calls without recalculating gates."


def selected_markers(accepted_markers, figure_id, options):
    """Resolve a bounded explicit marker set in the accepted panel's order."""
    from mif_pipeline.cell_analysis.coexpression_plots import MAX_MARKERS
    from urllib.parse import unquote
    if figure_id == 'coexpression/selected':
        values = (options or {}).get('markers')
        if (not isinstance(values, (list, tuple)) or not 2 <= len(values) <= MAX_MARKERS
                or any(type(value) is not str for value in values)
                or len(set(values)) != len(values) or not set(values) <= set(accepted_markers)):
            raise ValueError('Choose two to eight distinct accepted markers.')
        return tuple(marker for marker in accepted_markers if marker in values)
    return accepted_markers if figure_id == 'coexpression/all' else tuple(
        unquote(marker) for marker in figure_id.removeprefix('coexpression/pair/').split('/'))


def render_coexpression(authority, figure_id, snapshot, *, figure_options=None):
    from mif_pipeline.cell_analysis.coexpression_plots import count_marker_combinations, render_marker_combinations
    from .exports import load_spatial_handoff_with_artifacts

    gates = _accepted_gates(authority, snapshot)
    accepted_markers = _marker_ids(gates)
    groups = _rule_groups(gates)
    calls, _message = _choices(accepted_markers)
    choices = (*_rule_choices(groups), *calls)
    if figure_id not in {item.figure_id for item in choices}:
        raise ValueError("This marker combination figure is not available for the selected cell result.")
    ref = authority.artifact_ref
    identity = ref.owning_identity
    handoff, artifacts = load_spatial_handoff_with_artifacts(ref.locator)
    try:
        if (artifacts.descriptor.sha256 != identity["descriptor"]["sha256"]
                or artifacts.cells.sha256 != identity["cell_artifact"]["sha256"]
                or handoff.cell_count != identity["cell_count"]
                or handoff.selected_phenotype_view != "marker_rules"
                or handoff.phenotype_generation_id != identity["phenotype_generation_id"]
                or handoff.phenotype_inventory_sha256 != identity["phenotype_inventory_sha256"]
                or handoff.cell_selection_sha256 != identity["cell_selection_sha256"]):
            raise ValueError("The saved cells differ from the selected accepted result.")
        if figure_id.startswith('coexpression/rules/'):
            from mif_pipeline.cell_analysis.marker_rule_plots import count_rule_assignments, render_rule_assignments

            counts = count_rule_assignments(handoff.cell_dataset, groups)
            return render_rule_assignments(counts, page=int(figure_id.rsplit('/', 1)[1]))
        markers = selected_markers(accepted_markers, figure_id, figure_options)
        counts = count_marker_combinations(handoff.cell_dataset, markers)
        return render_marker_combinations(counts, export_palette=(figure_options or {}).get('export_palette', 'default'))
    finally:
        handoff.cell_dataset.close()
