"""Offline interventions, adversarial evidence and the unchanged correction locks."""

from copy import deepcopy
import json

import pytest

from or_agent.agent import run
from or_agent.experimental import (MultiExtraction, diagnose, diagnostic_feedback,
                                   evidence_status, read_multi, structured_gap)
from or_agent.models import Extraction, read_extraction


@pytest.mark.parametrize('attack', ['consumption', 'cost', 'capacity', 'transpose',
                                  'resource', 'task', 'unit'])
@pytest.mark.parametrize('policy', ['contradictions', 'verified'])
def test_source_fidelity_blocks_literal_but_wrong_data(attack, policy):
    source, data = fixture('single_excerpt')
    if attack == 'consumption': data['problem']['consumption'][0][0] = 3
    elif attack == 'cost': data['problem']['costs'][0][0] = 21  # Present elsewhere in the source.
    elif attack == 'capacity': data['problem']['capacities'][0] = 2
    elif attack == 'transpose': data['problem']['costs'] = [list(row) for row in zip(*data['problem']['costs'])]
    elif attack == 'resource': data['problem']['resources'].reverse()
    elif attack == 'task': data['problem']['tasks'].reverse()
    elif attack == 'unit': data['problem']['resource_unit'] = 'minute'
    result, requests = execute(source, [data], fidelity_policy=policy)
    assert result['outcome'] == 'abstain' and 'solver' not in result
    assert result['fidelity_check']['status'] == 'mismatch'
    assert result['fidelity'] == 'unassessed'
    assert result['fidelity_check']['differences']
    assert len(requests) == 1  # No repair of locked mathematical data.


@pytest.mark.parametrize('policy', ['contradictions', 'verified'])
@pytest.mark.parametrize('provenance', ['single_excerpt', 'multiple_excerpts'])
@pytest.mark.parametrize('diagnostics', ['validation_error', 'structured_diagnostics'])
def test_source_fidelity_complete_proof_reaches_real_solver(policy, provenance, diagnostics):
    source, data = fixture(provenance)
    result, requests = execute(source, [data], fidelity_policy=policy,
                               provenance=provenance, diagnostics=diagnostics)
    baseline, baseline_requests = execute(source, [data], provenance=provenance, diagnostics=diagnostics)
    assert requests == baseline_requests  # Prompts, schema and generation options unchanged.
    assert result['budgets'] == baseline['budgets']
    assert result['fidelity_check']['status'] == result['fidelity'] == 'verified'
    assert result['fidelity_check']['coverage'] == 'complete'
    assert result['candidate']['objective'] == 27 and result['optimality_proven']
    assert result['verification']['valid'] and result['explanation_valid']
    assert 'structured_gap_v1' in result['answer']


@pytest.mark.parametrize('case', ['extra_constraint', 'omitted_constraint', 'ambiguous',
                                'bare_value', 'incomplete_table', 'complex_prose',
                                'conflicting_values', 'partial_text', 'physical_unit'])
@pytest.mark.parametrize('policy', ['contradictions', 'verified'])
def test_source_fidelity_unknown_context_never_becomes_verified(case, policy):
    source, data = fixture('single_excerpt')
    if case in ('extra_constraint', 'omitted_constraint'): source += '\nT1 must be assigned to R2.'
    elif case == 'ambiguous': source = 'The following is an old hypothetical example.\n' + source
    elif case == 'bare_value': source = 'Costs: 14, 21, 23, 13; consumption: 2, 3; capacity: 3.'
    elif case == 'incomplete_table': source = source.replace('R2 costs: T1=23, T2=13.\n', '')
    elif case == 'complex_prose': source = 'Use the earlier rates, unless the night shift applies.'
    elif case == 'conflicting_values': source += '\nR1 capacity: 2.'
    elif case == 'partial_text': source += '\nThe following rows are examples, not operative data.'
    elif case == 'physical_unit': source = source.replace('Resource unit: slot.', 'Resource unit: unspecified physical meaning.')
    data['provenance'] = {field: source for field in data['problem']}
    result, _ = execute(source, [data], fidelity_policy=policy)
    assert result['fidelity_check']['status'] == result['fidelity'] == 'unassessed'
    assert result['fidelity_check']['coverage'] == 'none'
    assert result['fidelity_check']['checked_fields'] == []
    assert ('solver' in result) == (policy == 'contradictions')
    assert result['outcome'] == ('verified' if policy == 'contradictions' else 'abstain')
    assert 'Source fidelity is unassessed' in result['answer'] or policy == 'verified'


def test_source_fidelity_accepts_consistent_identifier_permutations():
    source, data = fixture('single_excerpt')
    problem = data['problem']
    problem['resources'].reverse()
    problem['tasks'].reverse()
    problem['capacities'].reverse()
    for field in ('costs', 'consumption'):
        problem[field] = [list(reversed(row)) for row in reversed(problem[field])]
    result, _ = execute(source, [data], fidelity_policy='verified')
    assert result['fidelity_check']['status'] == 'verified'
    assert result['candidate']['objective'] == 27


@pytest.mark.parametrize('case', ['infeasible', 'multiple_optima'])
def test_source_fidelity_preserves_solver_proofs(case):
    source, _ = fixture('single_excerpt')
    if case == 'infeasible': source = source.replace('capacity: 3.', 'capacity: 0.')
    else: source = source.replace('T1=14, T2=21', 'T1=1, T2=1').replace('T1=23, T2=13', 'T1=1, T2=1')
    problem, _ = structured_gap(source)
    data = dict(disposition='ready', problem=problem.model_dump(), issues=[],
                provenance={field: source for field in type(problem).model_fields})
    result, _ = execute(source, [data], fidelity_policy='verified')
    assert result['fidelity'] == 'verified' and result['explanation_valid']
    assert 'structured_gap_v1' in result['answer']
    if case == 'infeasible':
        assert result['outcome'] == 'infeasible' and result['candidate'] is None
        assert result['solver']['raw_status'] == 'INFEASIBLE' and not result['optimality_proven']
    else:
        assert result['candidate']['objective'] == 2 and result['optimality_proven']


def test_source_fidelity_requires_existing_provenance_validation():
    source, data = fixture('single_excerpt')
    data['provenance']['costs'] = 'not a literal quotation'
    result, _ = execute(source, [data, data], fidelity_policy='verified')
    assert 'fidelity_check' not in result and 'solver' not in result


def test_source_fidelity_does_not_bypass_p1_excerpt_coverage():
    source, data = fixture()
    data['provenance']['costs'] = data['provenance']['costs'][:1]
    result, _ = execute(source, [data], provenance='multiple_excerpts', fidelity_policy='verified')
    assert result['fidelity_check']['status'] == 'verified'
    assert result['fidelity'] == 'unassessed' and 'solver' not in result


def test_source_fidelity_policy_is_opt_in_and_validated():
    source, data = fixture('single_excerpt')
    data['problem']['consumption'][0][0] = 3
    result, _ = execute(source, [data])
    assert result['outcome'] == 'verified' and result['fidelity'] == 'unassessed'
    assert 'fidelity_check' not in result and 'experimental' not in result
    with pytest.raises(ValueError, match='fidelity_policy'):
        execute(source, [data], fidelity_policy='unknown')


@pytest.mark.parametrize('policy', ['contradictions', 'verified'])
@pytest.mark.parametrize('diagnostics', ['validation_error', 'structured_diagnostics'])
@pytest.mark.parametrize('fault', ['changed_cost', 'removed_constraint'])
def test_source_fidelity_preserves_correction_locks(policy, diagnostics, fault):
    source, good = fixture('single_excerpt')
    bad = deepcopy(good)
    bad['provenance']['costs'] = 'not a quotation'
    repaired = deepcopy(good)
    if fault == 'changed_cost': repaired['problem']['costs'][0][0] += 1
    else: bad['problem']['deadline'] = 7
    result, requests = execute(source, [bad, repaired], diagnostics=diagnostics, fidelity_policy=policy)
    assert result['reason'] == 'correction_changes_locked_data'
    assert len(requests) == 2 and 'solver' not in result and 'fidelity_check' not in result


@pytest.mark.parametrize('policy', ['contradictions', 'verified'])
@pytest.mark.parametrize('budget', ['max_calls', 'total_seconds'])
def test_source_fidelity_preserves_zero_budgets(policy, budget):
    source, data = fixture('single_excerpt')
    result, requests = execute(source, [data], fidelity_policy=policy, **{budget: 0})
    assert not requests and 'solver' not in result and 'fidelity_check' not in result


@pytest.mark.parametrize('policy', ['contradictions', 'verified'])
def test_source_fidelity_does_not_parse_hypothetical_data_out_of_context(policy):
    source, data = fixture('single_excerpt')
    source = 'The following is an obsolete example, not the operative specification.\n' + source
    data['problem']['consumption'][0][0] = 3
    result, _ = execute(source, [data], fidelity_policy=policy)
    assert result['fidelity_check']['status'] == 'unassessed'
    assert result['fidelity_check']['checked_fields'] == []


def fixture(provenance='multiple_excerpts'):
    problem = dict(tasks=['T1', 'T2'], resources=['R1', 'R2'],
                   costs=[[14, 21], [23, 13]], consumption=[[2, 3], [3, 2]],
                   capacities=[3, 3], cost_unit='credit', resource_unit='slot')
    quotes = dict(tasks=['Tasks: T1, T2.'], resources=['Resources: R1, R2.'],
                  costs=['R1 costs: T1=14, T2=21.', 'R2 costs: T1=23, T2=13.'],
                  consumption=['R1 consumption: T1=2, T2=3.', 'R2 consumption: T1=3, T2=2.'],
                  capacities=['R1 capacity: 3.', 'R2 capacity: 3.'],
                  cost_unit=['Cost unit: credit.'], resource_unit=['Resource unit: slot.'])
    source = 'Assign each task exactly once; minimize total cost within capacities.\n'
    source += '\n'.join(quote for values in quotes.values() for quote in values)
    if provenance == 'single_excerpt':
        quotes = {field: source for field in problem}
    return source, dict(disposition='ready', problem=problem, issues=[], provenance=quotes)


def execute(source, outputs, **parameters):
    requests = []
    responses = iter(outputs)

    def client(payload, remaining):
        requests.append(deepcopy(payload))
        output = next(responses)
        if isinstance(output, Exception):
            raise output
        return dict(message=dict(content=output if isinstance(output, str) else json.dumps(output)))

    result = run(source, client=client, admission=lambda: dict(allowed=True), **parameters)
    return result, requests


def test_multi_exact_dispersed_rows_and_separate_units():
    source, data = fixture()
    extracted = read_multi(json.dumps(data), source)
    assert extracted.problem.costs == [[14, 21], [23, 13]]
    assert extracted.provenance['costs'] == data['provenance']['costs']
    assert evidence_status(extracted, source) == dict(literal_authenticity=True, coverage='verified',
        fidelity='verified', operationally_admissible=True, scope='structured_gap_v1_only')
    assert not diagnose(json.dumps(data), source, 'multiple_excerpts')


def test_discontiguous_fragments_are_authentic_without_joining_source_text():
    source, data = fixture()
    first, second = data['provenance']['costs']
    source = source.replace(first + '\n' + second, first + '\nKeep every capacity constraint.\n' + second)
    assert read_multi(json.dumps(data), source).provenance['costs'] == [first, second]
    p0 = deepcopy(data)
    p0['provenance'] = {field: source for field in data['problem']}
    p0['provenance']['costs'] = first + '\n' + second
    with pytest.raises(ValueError, match='provenance for costs'):
        read_extraction(json.dumps(p0), source)


@pytest.mark.parametrize('mode', ['single', 'multiline', 'duplicates', 'overlap', 'reversed'])
def test_multi_cardinality_order_duplicates_and_overlap(mode):
    source, data = fixture()
    values = data['provenance']['costs']
    if mode == 'single': values[:] = [source]
    elif mode == 'multiline': values[:] = ['\n'.join(values)]
    elif mode == 'duplicates': values.append(values[0])
    elif mode == 'overlap': values.append(values[0][3:])
    elif mode == 'reversed': values.reverse()
    assert read_multi(json.dumps(data), source).provenance['costs'] == values


@pytest.mark.parametrize('value', [[], [''], ['  '], ['Absent'], ['r1 costs: T1=14, T2=21.'],
                                 ['R1 costs 14 and 21'], 'R1 costs: T1=14, T2=21.', [14]])
def test_multi_rejects_invalid_excerpts(value):
    source, data = fixture()
    data['provenance']['costs'] = value
    with pytest.raises(ValueError):
        read_multi(json.dumps(data), source)


def test_v1_contract_preserved_and_no_implicit_conversion():
    source, p0 = fixture('single_excerpt')
    assert read_extraction(json.dumps(p0), source).problem.costs[0] == [14, 21]
    with pytest.raises(ValueError): read_multi(json.dumps(p0), source)
    source, p1 = fixture()
    with pytest.raises(ValueError): read_extraction(json.dumps(p1), source)


@pytest.mark.parametrize('attack', ['wrong_field', 'wrong_resource', 'wrong_value', 'partial',
                                  'unit_only', 'constraint_deleted', 'unknown_constraint', 'missing_key'])
@pytest.mark.parametrize('diagnostics', ['validation_error', 'structured_diagnostics'])
def test_deceptive_literal_evidence_never_reaches_solver(attack, diagnostics):
    source, data = fixture()
    if attack == 'wrong_field': data['provenance']['costs'] = data['provenance']['consumption']
    elif attack == 'wrong_resource': data['problem']['costs'].reverse()
    elif attack == 'wrong_value': data['problem']['costs'][0][0] = 12
    elif attack == 'partial': data['provenance']['costs'] = data['provenance']['costs'][:1]
    elif attack == 'unit_only': data['provenance'] = {field: ['credit'] for field in data['problem']}
    elif attack == 'constraint_deleted':
        source += '\nT1 must be assigned to R2.'
    elif attack == 'unknown_constraint': data['problem']['deadline'] = 4
    elif attack == 'missing_key': del data['provenance']['capacities']
    result, _ = execute(source, [data, data], provenance='multiple_excerpts', diagnostics=diagnostics)
    assert result['outcome'] == 'abstain'
    assert result['candidate'] is None and 'solver' not in result
    assert result['fidelity'] == 'unassessed'


@pytest.mark.parametrize('provenance', ['single_excerpt', 'multiple_excerpts'])
def test_complete_diagnostic_independent_fields_and_missing_keys(provenance):
    source, data = fixture(provenance)
    for field in ('costs', 'capacities'):
        data['provenance'][field] = 'invented' if provenance == 'single_excerpt' else ['invented']
    del data['provenance']['cost_unit']
    errors = diagnose(json.dumps(data), source, provenance, locked=True)
    quotes = [error for error in errors if error['category'] == 'provenance']
    assert {error['field'].split('[')[0] for error in quotes} == {'costs', 'capacities', 'cost_unit'}
    assert all(not error['locked'] and error['blocking'] for error in quotes)
    assert all('source' in error['allowed_action'] for error in quotes)


@pytest.mark.parametrize('diagnostics', ['validation_error', 'structured_diagnostics'])
def test_feedback_only_changes_after_common_first_request(diagnostics):
    source, good = fixture('single_excerpt')
    bad = deepcopy(good)
    bad['provenance']['costs'] = 'bad costs'
    bad['provenance']['capacities'] = 'bad capacities'
    result, requests = execute(source, [bad, good], diagnostics=diagnostics)
    assert result['outcome'] == 'verified'
    assert result['candidate']['objective'] == 27
    assert len(requests) == 2
    assert requests[0]['messages'][-1]['content'] == source
    feedback = requests[1]['messages'][-1]['content']
    assert 'costs' in feedback
    if diagnostics == 'structured_diagnostics':
        assert len(result['attempts'][0]['diagnostics']) == 2
        assert 'capacities' in feedback and 'allowed_action' in feedback
    else:
        assert 'provenance for capacities' not in feedback
        assert 'diagnostics' not in result['attempts'][0]


@pytest.mark.parametrize('provenance', ['single_excerpt', 'multiple_excerpts'])
@pytest.mark.parametrize('field', ['capacities', 'costs', 'consumption', 'resources', 'deadline'])
def test_d1_preserves_every_lock(provenance, field):
    source, good = fixture(provenance)
    bad = deepcopy(good)
    bad['provenance']['costs'] = 'bad' if provenance == 'single_excerpt' else ['bad']
    if field == 'deadline': bad['problem']['deadline'] = 5
    repaired = deepcopy(good)
    if field == 'capacities': repaired['problem'][field][0] += 1
    elif field in ('costs', 'consumption'): repaired['problem'][field][0][0] += 1
    elif field == 'resources': repaired['problem'][field].reverse()
    result, _ = execute(source, [bad, repaired], provenance=provenance, diagnostics='structured_diagnostics')
    assert result['reason'] == 'correction_changes_locked_data'
    assert result['candidate'] is None


@pytest.mark.parametrize('output', ['{', 'same', 'nonready', 'timeout'])
@pytest.mark.parametrize('provenance', ['single_excerpt', 'multiple_excerpts'])
def test_d1_stopping_and_no_unbounded_retry(output, provenance):
    source, data = fixture(provenance)
    if output == 'nonready':
        data = dict(disposition='clarify', problem=None, issues=['Which capacity?'], provenance={})
    else:
        data['provenance']['costs'] = 'bad' if provenance == 'single_excerpt' else ['bad']
    reply = '{' if output == '{' else TimeoutError('test timeout') if output == 'timeout' else data
    result, requests = execute(source, [reply, reply], provenance=provenance, diagnostics='structured_diagnostics')
    assert len(requests) <= 2 and result['candidate'] is None
    assert result['outcome'] == ('clarify' if output == 'nonready' else
                                 'execution_error' if output == 'timeout' else 'abstain')


def test_d1_no_call_when_budget_zero_and_no_reference_input():
    source, data = fixture('single_excerpt')
    result, requests = execute(source, [], diagnostics='structured_diagnostics', max_calls=0)
    assert result['reason'] == 'call_budget_exhausted' and not requests
    errors = diagnose('{', source)
    assert len(errors) == 1 and errors[0]['category'] == 'syntax'
    assert set(MultiExtraction.model_fields) == set(Extraction.model_fields)


def test_d1_reports_independent_shapes_types_and_quotes_together():
    source, data = fixture('single_excerpt')
    data['problem']['capacities'] = [3]
    data['problem']['costs'] = [[14]]
    data['problem']['consumption'][0][0] = '2'
    data['provenance']['cost_unit'] = 'not a source quote'
    errors = diagnose(json.dumps(data), source, locked=True)
    fields = {error['field'] for error in errors}
    assert {'problem.capacities', 'problem.costs', 'problem.consumption.0.0', 'cost_unit'} <= fields
    assert all(error['locked'] for error in errors if error['field'].startswith('problem'))


def test_diagnostics_do_not_change_first_payload_or_fixed_budgets():
    source, data = fixture('single_excerpt')
    initial = []
    for diagnostics in ('validation_error', 'structured_diagnostics'):
        result, requests = execute(source, [data], diagnostics=diagnostics)
        initial.append(requests[0])
        assert result['budgets'] == dict(max_calls=2, total_seconds=180.0, solver_seconds=5.0)
    assert initial[0] == initial[1]


def test_d1_unknown_root_constraint_is_nonrecoverable():
    source, data = fixture('single_excerpt')
    data['deadline'] = 7
    result, requests = execute(source, [data], diagnostics='structured_diagnostics')
    assert result['reason'] == 'unknown_extraction_fields_cannot_be_removed'
    assert len(requests) == 1 and result['candidate'] is None


def test_combined_repair_admits_only_the_verified_structured_subset():
    source, good = fixture()
    bad = deepcopy(good)
    bad['provenance']['costs'] = ['not a quote']
    bad['provenance']['capacities'] = ['not a quote']
    result, requests = execute(source, [bad, good], provenance='multiple_excerpts', diagnostics='structured_diagnostics')
    assert len(requests) == 2
    assert len(result['attempts'][0]['diagnostics']) == 2
    assert result['attempts'][1]['structural_valid']
    assert result['evidence']['coverage'] == 'verified'
    assert result['outcome'] == 'verified' and result['candidate']['objective'] == 27
    assert 'structured_gap_v1' in result['answer']


def test_d1_fixes_only_diagnosed_fields_with_coherent_controlled_client():
    source, good = fixture('single_excerpt')
    bad = deepcopy(good)
    bad['provenance']['costs'] = 'bad costs'
    bad['provenance']['capacities'] = 'bad capacities'
    results = {}
    for diagnostics in ('validation_error', 'structured_diagnostics'):
        def client(payload, remaining):
            reply = deepcopy(bad)
            if len(payload['messages']) > 2:
                feedback = payload['messages'][-1]['content']
                for field in ('costs', 'capacities'):
                    if (f'provenance for {field}' in feedback or f'"field": "{field}"' in feedback):
                        reply['provenance'][field] = good['provenance'][field]
            return dict(message=dict(content=json.dumps(reply)))
        results[diagnostics] = run(source, diagnostics=diagnostics, client=client,
                                   admission=lambda: dict(allowed=True))
    assert results['validation_error']['outcome'] == 'abstain'
    assert results['structured_diagnostics']['outcome'] == 'verified'
    assert all(len(result['attempts']) == 2 for result in results.values())


def test_noninteger_api_exposure_uses_exact_integer_evidence():
    source, data = fixture('single_excerpt')
    data['problem']['costs'][1][1] = 12
    source = source.replace('T2=13', 'T2=12')
    data['provenance'] = {field: source for field in data['problem']}
    result, requests = execute(source, [data], diagnostics='structured_diagnostics')
    assert result['outcome'] == 'verified' and result['candidate']['objective'] == 26
    assert result['verification']['recomputed_objective'] == 26
    assert result['solver']['raw_objective'] == result['solver']['raw_best_bound'] == 26.000000000000004
    assert result['solver']['best_bound'] == 26 and result['optimality_proven']
    assert len(requests) == 1


@pytest.mark.parametrize('mode', ['separate', 'multiline', 'duplicate', 'reordered_rows'])
@pytest.mark.parametrize('diagnostics', ['validation_error', 'structured_diagnostics'])
def test_structured_p1_reaches_solver_and_independent_optimum(mode, diagnostics):
    import itertools

    source, data = fixture()
    if mode == 'multiline':
        data['provenance']['costs'] = ['\n'.join(data['provenance']['costs'])]
    elif mode == 'duplicate':
        data['provenance']['costs'] *= 2
    elif mode == 'reordered_rows':
        lines = source.splitlines()
        source = lines[0] + '\n' + '\n'.join(reversed(lines[1:]))
    reconstructed, _ = structured_gap(source)
    assert reconstructed.model_dump() == data['problem']
    p = data['problem']
    feasible = [sum(p['costs'][i][j] for j, i in enumerate(a))
                for a in itertools.product(range(2), repeat=2)
                if all(sum(p['consumption'][i][j] for j, r in enumerate(a) if r == i)
                       <= p['capacities'][i] for i in range(2))]
    result, requests = execute(source, [data], provenance='multiple_excerpts', diagnostics=diagnostics)
    assert result['outcome'] == 'verified'
    assert result['candidate']['objective'] == min(feasible) == 27
    assert result['verification']['valid'] and result['optimality_proven']
    assert result['solver']['raw_status'] == 'OPTIMAL' and result['explanation_valid']
    assert result['fidelity'] == 'verified'
    assert 'no general language guarantee' in result['answer']
    assert len(requests) == 1


@pytest.mark.parametrize('attack', ['capacity', 'cost', 'consumption', 'units', 'task_order',
    'resource_order', 'transpose', 'cross_field', 'partial_line', 'missing_row', 'whole_source',
    'missing_quote', 'nonliteral', 'extra_constraint', 'ambiguous', 'unsupported',
    'duplicate_declaration', 'duplicate_row', 'missing_source_value', 'extra_task_pair',
    'unknown_resource', 'unknown_column', 'missing_directive'])
def test_structured_p1_rejects_incomplete_or_misattributed_data(attack):
    source, data = fixture()
    if attack == 'capacity': data['problem']['capacities'][0] = 4
    elif attack == 'cost': data['problem']['costs'][0][0] = 999
    elif attack == 'consumption': data['problem']['consumption'][0][0] = 1
    elif attack == 'units': data['problem']['cost_unit'] = 'slot'
    elif attack == 'task_order': data['problem']['tasks'].reverse()
    elif attack == 'resource_order': data['problem']['resources'].reverse()
    elif attack == 'transpose': data['problem']['costs'] = [[14, 23], [21, 13]]
    elif attack == 'cross_field': data['provenance']['costs'] = data['provenance']['consumption']
    elif attack == 'partial_line': data['provenance']['costs'][0] = 'T1=14'
    elif attack == 'missing_row': data['provenance']['costs'].pop()
    elif attack == 'whole_source': data['provenance'] = {f: [source] for f in data['problem']}
    elif attack == 'missing_quote': del data['provenance']['capacities']
    elif attack == 'nonliteral': data['provenance']['costs'][0] += 'invented'
    elif attack == 'extra_constraint': source += '\nT1 must be assigned to R2.'
    elif attack == 'ambiguous': source += '\nR1 capacity: 4.'
    elif attack == 'unsupported': source = 'Please solve this GAP:\n' + source
    elif attack == 'duplicate_declaration': source += '\nTasks: T1, T2.'
    elif attack == 'duplicate_row': source += '\nR1 costs: T1=14, T2=21.'
    elif attack == 'missing_source_value': source = source.replace('R1 capacity: 3.', '')
    elif attack == 'extra_task_pair': source = source.replace('T2=21.', 'T2=21, T3=7.')
    elif attack == 'unknown_resource': source += '\nR3 capacity: 9.'
    elif attack == 'unknown_column': source = source.replace('T2=21.', 'T3=21.')
    elif attack == 'missing_directive': source = '\n'.join(source.splitlines()[1:])
    result, _ = execute(source, [data, data], provenance='multiple_excerpts', diagnostics='structured_diagnostics')
    assert result['outcome'] == 'abstain' and result['candidate'] is None
    assert result['fidelity'] == 'unassessed' and 'solver' not in result


def test_structured_p1_infeasible_without_relaxation():
    source, data = fixture()
    source = source.replace('capacity: 3.', 'capacity: 0.')
    data['problem']['capacities'] = [0, 0]
    data['provenance']['capacities'] = ['R1 capacity: 0.', 'R2 capacity: 0.']
    result, requests = execute(source, [data], provenance='multiple_excerpts')
    assert result['outcome'] == 'infeasible' and result['fidelity'] == 'verified'
    assert result['solver']['raw_status'] == 'INFEASIBLE'
    assert result['candidate'] is None and not result['optimality_proven']
    assert result['explanation_valid'] and len(requests) == 1


def test_structured_p1_feasible_does_not_claim_optimality(monkeypatch):
    from or_agent import agent
    source, data = fixture()
    monkeypatch.setattr(agent, 'solve', lambda *args: dict(candidate=dict(objective=27,
        assignments=[dict(task='T1', resource='R1'), dict(task='T2', resource='R2')]),
        raw_status='FEASIBLE', solver_proof=False))
    result, _ = execute(source, [data], provenance='multiple_excerpts')
    assert result['outcome'] == 'verified' and not result['optimality_proven']
    assert 'optimality unproven' in result['answer'] and result['explanation_valid']


@pytest.mark.parametrize('field,value', [('costs', [[True, 21], [23, 13]]),
    ('consumption', [[-1, 3], [3, 2]]), ('capacities', [3.0, 3])])
def test_d1_numeric_errors_and_quotes_are_independent(field, value):
    source, data = fixture('single_excerpt')
    data['problem'][field] = value
    data['provenance']['cost_unit'] = 'missing source quote'
    data['provenance']['unknown'] = source
    del data['provenance']['resource_unit']
    errors = diagnose(json.dumps(data), source, locked=True)
    assert any(e['field'].startswith('problem.' + field) and e['locked'] for e in errors)
    assert {'cost_unit', 'unknown', 'resource_unit'} <= {e['field'] for e in errors}
    feedback = diagnostic_feedback(errors)
    assert len(feedback) == len(errors)
    assert all({k: v for k, v in e.items() if k not in ('value', 'blocking')} == f
               for e, f in zip(errors, feedback))
    assert len(json.dumps(feedback)) < len(json.dumps(errors))


def test_d1_partial_repair_keeps_remaining_errors_and_stops():
    source, good = fixture('single_excerpt')
    first, partial = deepcopy(good), deepcopy(good)
    first['provenance']['costs'] = 'bad costs'
    first['provenance']['capacities'] = partial['provenance']['capacities'] = 'bad capacities'
    result, requests = execute(source, [first, partial], diagnostics='structured_diagnostics')
    assert len(result['attempts'][0]['diagnostics']) == 2
    assert [e['field'] for e in result['attempts'][1]['diagnostics']] == ['capacities']
    assert result['outcome'] == 'abstain' and len(requests) == 2
    assert result['reason'] == 'invalid_extraction_call_budget_exhausted'


def test_p1_elapsed_budget_prevents_generation():
    source, _ = fixture()
    result, requests = execute(source, [], provenance='multiple_excerpts', diagnostics='structured_diagnostics', total_seconds=0)
    assert not requests and result['reason'] == 'elapsed_budget_exhausted'
