"""Explain accepted phenotype assignments from saved calls and their ordered rules."""
from collections import defaultdict
import json
import textwrap

import numpy as np

from .marker_rules import _rule_match
from .phenotyping_population import NOT_PHENOTYPED
from .result_plots import COMPOSITION_PAGE_SIZE, _csv_output, _exports


def group_rule_definitions(definitions):
    """Pool sections only when their exact ordered predicates and residual agree."""
    grouped = {}
    for scope, (rules, residual) in sorted(definitions.items()):
        key = (tuple((rule.phenotype, tuple(sorted(rule.marker_states.items())), rule.precedence)
                     for rule in rules), residual)
        if key not in grouped:
            grouped[key] = {'rules': tuple(rules), 'residual': residual, 'scopes': []}
        grouped[key]['scopes'].append(scope)
    return tuple(grouped.values())


def rule_expression(rule):
    return ' AND '.join(marker + ('+' if state == 'positive' else '−')
                        for marker, state in rule.marker_states.items() if state != 'ignore')


def count_rule_assignments(dataset, groups):
    """Read selected columns once; retain bounded rule counters, without geometry."""
    scopes = {scope: index for index, group in enumerate(groups) for scope in group['scopes']}
    labels = dataset.dictionaries['primary_phenotype'].labels
    counters = [np.zeros((len(group['rules']), 4), dtype=np.int64) for group in groups]
    selected = [0] * len(groups)
    residuals = [0] * len(groups)
    total = excluded = 0
    columns = ('slide_id', 'section_id', 'primary_phenotype', 'marker_states')
    for batch in dataset.iter_batches(columns=columns):
        rows = batch.to_pylist()
        by_group = defaultdict(list)
        details = {}
        for index, row in enumerate(rows):
            total += 1
            code = row['primary_phenotype']
            if code is None or not 0 <= code < len(labels):
                raise ValueError('A saved cell has no accepted phenotype label.')
            phenotype = labels[code]
            group_index = scopes.get((row['slide_id'], row['section_id']))
            if group_index is None:
                raise ValueError('The saved cell has no matching accepted marker definition.')
            details[index] = phenotype
            if phenotype == NOT_PHENOTYPED:
                excluded += 1
            else:
                by_group[group_index].append(index)
        for group_index, indices in by_group.items():
            group = groups[group_index]
            rules = group['rules']
            markers = tuple(rules[0].marker_states)
            marker_indices = {marker: index for index, marker in enumerate(markers)}
            matrix = np.full((len(indices), len(markers)), -1, dtype=np.int8)
            for row_index, index in enumerate(indices):
                states = json.loads(rows[index]['marker_states'])
                for marker, column in marker_indices.items():
                    state = states.get(marker, 'unknown')
                    if state not in {'positive', 'negative', 'unknown'}:
                        raise ValueError('Saved marker calls contain an unsupported state.')
                    matrix[row_index, column] = {'positive': 1, 'negative': 0, 'unknown': -1}[state]
            selected[group_index] += len(indices)
            first_match = [None] * len(indices)
            for rule_index, rule in enumerate(rules):
                matched = _rule_match(matrix, marker_indices, rule)
                required = [marker_indices[marker] for marker, state in rule.marker_states.items() if state != 'ignore']
                unknown = np.any(matrix[:, required] == -1, axis=1)
                assigned = 0
                for local, index in enumerate(indices):
                    phenotype = details[index]
                    if matched[local]:
                        if first_match[local] is None:
                            first_match[local] = rule.phenotype
                    assigned += phenotype == rule.phenotype
                match_count = int(matched.sum())
                counters[group_index][rule_index] += (match_count, assigned, match_count - assigned, int(unknown.sum()))
            for local, index in enumerate(indices):
                actual = details[index]
                if actual != (first_match[local] or group['residual']):
                    raise ValueError('The saved phenotype does not match its accepted marker calls and rule order.')
                residuals[group_index] += actual == group['residual']
    if total != dataset.row_count:
        raise ValueError('Saved marker-call rows differ from the selected cell count.')
    summary = tuple({'group': group_index + 1, 'phenotype': rule.phenotype,
                     'expression': rule_expression(rule), 'order': rule_index + 1,
                     'matched': int(counts[0]), 'assigned': int(counts[1]),
                     'overlap': int(counts[2]), 'unknown': int(counts[3]),
                     'selected': selected[group_index]}
                    for group_index, group in enumerate(groups)
                    for rule_index, (rule, counts) in enumerate(zip(group['rules'], counters[group_index], strict=True)))
    return {'rows': summary, 'total': total, 'selected': sum(selected), 'excluded': excluded,
            'residuals': tuple(residuals), 'selected_by_group': tuple(selected),
            'groups': groups}


def render_rule_assignments(result, *, page=0):
    """Show rule overlap separately from unknown calls; export rule totals."""
    from matplotlib.figure import Figure
    from matplotlib.ticker import MaxNLocator

    start = page * COMPOSITION_PAGE_SIZE
    rows = result['rows'][start:start + COMPOSITION_PAGE_SIZE]
    if not rows:
        raise ValueError('This phenotype rule page is unavailable.')
    multiple = len(result['groups']) > 1
    titles = [textwrap.fill((f"Rule set {row['group']} · " if multiple else '') + row['phenotype'], 30)
              + '\n' + textwrap.fill(row['expression'], 42) for row in rows]
    height_pt = 10 * sum(title.count('\n') + 1 for title in titles) + 92
    figure = Figure(figsize=(183 / 25.4, max(142, height_pt) / 72), layout='constrained')
    figure.get_layout_engine().set(w_pad=.16, h_pad=.045)
    axis, right = figure.subplots(1, 2, sharey=True, gridspec_kw={'width_ratios': (6, 1)})
    positions = np.arange(len(rows))
    assigned = [row['assigned'] for row in rows]
    overlap = [row['overlap'] for row in rows]
    axis.barh(positions, assigned, color='#146b78', label='Assigned to this phenotype')
    axis.barh(positions, overlap, left=assigned, color='#d39a4a', label='Matched, assigned by an earlier rule')
    maximum = max(1, *(row['matched'] for row in rows))
    for position, row in enumerate(rows):
        axis.text(row['matched'] + maximum * .02, position,
                  f"{row['matched']:,} {'match' if row['matched'] == 1 else 'matches'}", va='center', fontsize=7)
    axis.set_yticks(positions, labels=titles, fontsize=7)
    for label in axis.get_yticklabels():
        label.set_parse_math(False)
    axis.invert_yaxis()
    axis.tick_params(axis='y', length=0, pad=13)
    axis.set_xlim(0, maximum * 1.4)
    axis.xaxis.set_major_locator(MaxNLocator(integer=True))
    axis.set_xlabel('Cells matching each rule', fontsize=7)
    axis.tick_params(labelsize=7, length=2, width=.5)
    axis.spines['bottom'].set_linewidth(.5)
    axis.spines[['top', 'right', 'left']].set_visible(False)
    axis.legend(loc='lower left', bbox_to_anchor=(0, 1.01), frameon=False, fontsize=7)
    # Keep unavailable-call counts in a dedicated aligned column.
    right.set_xlim(0, 1)
    right.set_xticks([])
    right.tick_params(axis='y', left=False, labelleft=False)
    right.spines[:].set_visible(False)
    for position, row in enumerate(rows):
        right.text(.5, position, f"{row['unknown']:,}", ha='center', va='center', fontsize=7)
    right.text(.5, 1.04, 'Unknown required\nmarkers (cells)',
               transform=right.transAxes, ha='center', va='bottom', fontsize=7)
    figure.supxlabel(f"Selected cells: {result['selected']:,}  ·  Unmatched: {sum(result['residuals']):,}"
                     f"  ·  Not phenotyped: {result['excluded']:,}\n"
                     'Match counts can overlap. Unknown required markers are counted separately.', fontsize=7)
    columns = ('row_type', 'rule_set', 'phenotype', 'rule_order', 'required_states', 'total_matches',
               'assigned_cells', 'matched_assigned_earlier', 'unknown_required_markers', 'selected_cells')
    data = [('rule', row['group'], row['phenotype'], row['order'], row['expression'],
             row['matched'], row['assigned'], row['overlap'], row['unknown'], row['selected'])
            for row in result['rows']]
    data.extend(('unmatched', index + 1, group['residual'], None, None, None, result['residuals'][index],
                 None, None, result['selected_by_group'][index]) for index, group in enumerate(result['groups']))
    data.append(('excluded', None, NOT_PHENOTYPED, None, None, None, result['excluded'], None, None, result['selected']))
    return (*_exports(figure, 'phenotype-rule-assignments', 'Phenotype rule assignments'),
            _csv_output('phenotype-rule-summary', 'Phenotype rule summary', columns, data))
