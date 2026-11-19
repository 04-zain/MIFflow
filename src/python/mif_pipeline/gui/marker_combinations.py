"""The single post-gate phenotype combination form."""
from collections.abc import Mapping
from mif_pipeline.gui.interactions import ControlKind, FormControl, FormModel, decode_form_values
from mif_pipeline.pipeline_core import StageResult
from mif_pipeline.orchestration.batch_phenotyping.marker_combinations import (
    UNMATCHED_PHENOTYPE, validate_combination_context, validate_combination_response,
)


def build_marker_combination_form(result):
    context = validate_combination_context(result)
    initial = context['initial_definition'] or {}
    return FormModel(form_id='marker_combinations',title='Define phenotype combinations',
        message='', allowed_actions=result.allowed_actions,
        controls=(FormControl('marker_composite_names','Phenotypes in assignment order',ControlKind.OBJECT_SEQUENCE,
                    value=tuple(rule['phenotype_id'] for rule in initial.get('composite_rules',()))),),
        context={'adapter':'marker_combinations','stage_result':result.to_mapping(),
                 'plain_text_sequences':('marker_composite_names',),
                 'initial_marker_states':{rule['phenotype_id']:rule['marker_states'] for rule in initial.get('composite_rules',())}})


def submit_marker_combination_form(form, action, raw_values):
    if action == 'abort' and action in form.allowed_actions:
        return {}
    if set(raw_values)-{'marker_composite_names','marker_composite_states'}:
        raise ValueError('Phenotype combinations contain fields that were not offered')
    values = decode_form_values(form,action,{k:v for k,v in raw_values.items() if k!='marker_composite_states'})
    result = StageResult.from_mapping(form.context['stage_result'])
    markers = tuple(validate_combination_context(result)['canonical_marker_ids'])
    names = values.get("marker_composite_names")
    if (
        not isinstance(names, list)
        or not names
        or any(type(name) is not str or not name or name != name.strip() for name in names)
        or len({name.casefold() for name in names}) != len(names)
    ):
        raise ValueError("Composite phenotype names must be distinct and nonblank")
    residual = UNMATCHED_PHENOTYPE
    if residual.casefold() in {name.casefold() for name in names}:
        raise ValueError(f"{residual} is reserved for cells that match no phenotype rule.")
    raw_states = raw_values.get("marker_composite_states")
    if not isinstance(raw_states, (list, tuple)) or len(raw_states) != len(names):
        raise ValueError("Each phenotype requires its offered marker-state selectors")
    composites = []
    for name, row in zip(names, raw_states):
        if (
            not isinstance(row, Mapping)
            or set(row) != {"phenotype_id", "marker_states"}
            or row["phenotype_id"] != name
        ):
            raise ValueError("Phenotype marker states do not match ordered names")
        states = row["marker_states"]
        if (
            not isinstance(states, Mapping)
            or set(states) != set(markers)
            or any(state not in {"positive", "negative", "not_used"} for state in states.values())
        ):
            raise ValueError("Phenotype marker states differ from offered markers")
        composites.append(
            {
                "phenotype_id": name,
                "marker_states": {
                    marker: state for marker, state in states.items() if state != "not_used"
                },
            }
        )
    definition = {'phenotype_classes':[{'class_id':name,'display_name':name} for name in (*names,residual)],
                  'composite_rules':composites,'residual_phenotype_id':residual}
    return validate_combination_response(result,{'phenotype_definition':definition})
