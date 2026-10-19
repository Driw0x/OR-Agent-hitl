"""Offline source proofs and simulated hybrid outputs. No Ollama generation."""

from copy import deepcopy
import json
import socket

import pytest

from or_agent import agent, deterministic as d
from or_agent.models import Gap
from test_crosscheck import fixture as p1_fixture


def fixture():
    source = ('At the weaving workshop, ' + d.GAP_SENTENCES + '\n'
        'Tasks: Weave,Dye.\nResources: LoomA,LoomB.\n'
        'Cost unit: euro. Resource unit: minute.\n'
        'Cumulative capacity of LoomA: 2 minute.\nCumulative capacity of LoomB: 3 minute.\n'
        'Assignment record: T=Weave; R=LoomB; cost=4 euro; consumption=1 minute.\n'
        'Assignment record: T=Dye; R=LoomA; cost=3 euro; consumption=2 minute.\n'
        'Assignment record: T=Weave; R=LoomA; cost=-2 euro; consumption=0 minute.\n'
        'Assignment record: T=Dye; R=LoomB; cost=7 euro; consumption=2 minute.')
    problem = dict(tasks=['Weave', 'Dye'], resources=['LoomA', 'LoomB'], costs=[[-2, 3], [4, 7]],
                   consumption=[[0, 2], [1, 2]], capacities=[2, 3], cost_unit='euro', resource_unit='minute')
    output = dict(disposition='ready', problem=problem, issues=[], provenance={f: source for f in problem})
    return source, output


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('offline test attempted network access')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


def simulated(source, output, mode='locked_facts'):
    calls = []
    def client(payload, timeout):
        calls.append(payload)
        return dict(message=dict(content=output if isinstance(output, str) else json.dumps(output)))
    result = d.run(source, mode, client=client, admission=lambda: dict(allowed=True))
    assert len(calls) == (0 if mode == 'deterministic' else 1)
    return result


def test_complete_records_exact_source():
    source, output = fixture()
    report = d.extract(source)
    assert report['complete_problem'] == output['problem']
    assert report['complete_proof'] == 'closed_labelled_records_v1'
    assert len(report['facts']) == 10
    assert all(f['verification'] == 'verified' and source[f['start']:f['end']] == f['evidence'] for f in report['facts'])
    assert not report['uninterpreted']


def test_reuses_complete_p1():
    source, output = p1_fixture()
    report = d.extract(source)
    assert report['complete_proof'] == 'structured_gap_v1'
    assert report['complete_problem'] == output['problem']


@pytest.mark.parametrize('line', [
    'Assignment record: T=Dye; R=LoomB; cost=7 euro; consumption=2 minute.',
    'Cumulative capacity of LoomB: 3 minute.',
    'Tasks: Weave,Dye.', 'Resources: LoomA,LoomB.',
    'Cost unit: euro. Resource unit: minute.',
])
def test_missing_data_preserves_partial_facts(line):
    source, _ = fixture()
    report = d.extract(source.replace(line, ''))
    assert report['complete_problem'] is None
    assert report['facts']
    assert any(row['reason'] for row in report['unknown'])


@pytest.mark.parametrize('suffix', [
    'LoomA Dye -2 minute', 'LoomA and Dye have numbers 7 and 2.',
    'Costs table (task rows): Weave -2 4; Dye 3 7.',
    'LoomA costs Dye=3; Weave=-2.', 'Dye takes two minutes on LoomA.',
])
def test_ambiguous_unrecognized_and_transposed_table(suffix):
    report = d.extract(suffix)
    assert not report['facts'] and report['uninterpreted'] and report['complete_problem'] is None


def test_partial_p1_table_and_transposed_llm():
    source, output = p1_fixture()
    partial = '\n'.join(line for line in source.splitlines() if not line.startswith('R2 consumption:'))
    report = d.extract(partial)
    assert report['coverage']['consumption'] == dict(expected=4, verified=2)
    assert report['complete_problem'] is None
    changed = deepcopy(output['problem'])
    changed['costs'] = [list(row) for row in zip(*changed['costs'])]
    assert not d.check_locks(source, changed)['accepted']


@pytest.mark.parametrize('change', ['value', 'unit', 'tasks', 'resources'])
def test_explicit_contradictions(change):
    source, _ = fixture()
    if change == 'value':
        source += '\nCumulative capacity of LoomA: 5 minute.'
    elif change == 'unit':
        source = source.replace('cost=-2 euro', 'cost=-2 dollar')
    else:
        source += '\n' + ('Tasks: Weave,Spin.' if change == 'tasks' else 'Resources: LoomA,LoomC.')
    report = d.extract(source)
    assert report['contradictions'] and report['complete_problem'] is None


def test_numeric_value_wrong_field_does_not_prove_association():
    source, output = fixture()
    source = source.replace('Cumulative capacity of LoomA: 2 minute.', 'LoomA cost is 2 minute.')
    report = d.extract(source)
    assert ('capacities', 'LoomA', None) not in {(f['field'], f['resource'], f['task']) for f in report['facts']}
    assert report['coverage']['capacities']['verified'] == 1


@pytest.mark.parametrize('value', ['1.0', 'true', '01', '1e2', '+1', 'NaN'])
def test_nonstrict_source_number_not_recognized(value):
    source, _ = fixture()
    report = d.extract(source.replace('cost=-2 euro', 'cost=' + value + ' euro'))
    assert report['coverage']['costs']['verified'] == 3
    assert report['complete_problem'] is None


@pytest.mark.parametrize('value', ['-1', '1000000001'])
def test_invalid_domain_never_clamped(value):
    source, _ = fixture()
    report = d.extract(source.replace('LoomA: 2 minute.', 'LoomA: ' + value + ' minute.'))
    fact = next(f for f in report['facts'] if f['field'] == 'capacities' and f['resource'] == 'LoomA')
    assert fact['value'] == int(value) and report['contradictions'] and report['complete_problem'] is None


@pytest.mark.parametrize('extra', ['Deadline for Dye: 5 minute.', 'There may be another mandatory rule.', '', ' '])
def test_additional_or_uninterpreted_line_blocks_s2(extra):
    source, _ = fixture()
    report = d.extract(source + '\n' + extra + '\n')
    assert report['complete_problem'] is None and report['uninterpreted']


def test_unit_absent_p1_is_partial():
    source, _ = p1_fixture()
    report = d.extract(source.replace('Resource unit: minute.', ''))
    assert any(f['verification'] == 'unknown' for f in report['facts'])
    assert report['complete_problem'] is None


def test_verified_numeric_facts_do_not_prove_extra_prose():
    source, _ = fixture()
    report = d.extract('A remark about scheduling without any requested schedule.\n' + source)
    assert len(report['facts']) == 10 and report['complete_problem'] is None


def test_duplicate_identical_facts_are_not_contradictions():
    source, _ = fixture()
    report = d.extract(source + '\nCumulative capacity of LoomA: 2 minute.')
    assert not report['contradictions'] and report['complete_problem'] is not None


@pytest.mark.parametrize('field', ['costs', 'consumption', 'capacities', 'cost_unit', 'resource_unit', 'tasks', 'resources'])
def test_locked_changes_rejected_before_solver(field, monkeypatch):
    source, output = fixture()
    original = deepcopy(output)
    if field in ('costs', 'consumption'):
        output['problem'][field][0][0] += 1
    elif field == 'capacities':
        output['problem'][field][0] += 1
    elif field.endswith('unit'):
        output['problem'][field] = 'other'
    else:
        output['problem'][field][0] = 'Other'
    def forbidden(*args):
        pytest.fail('locked change reached CP-SAT')
    monkeypatch.setattr(agent, 'solve', forbidden)
    result = simulated(source, output)
    assert result['outcome'] == 'abstain' and result['reason'] == 'locked_fact_mismatch'
    assert result['lock_check']['differences']
    assert json.loads(result['attempts'][0]['raw']) == output
    assert original['problem'] != output['problem']


@pytest.mark.parametrize('change', ['missing', 'extra', 'incomplete', 'json', 'float', 'bool'])
def test_invalid_outputs_abstain(change):
    source, output = fixture()
    if change == 'missing':
        del output['problem']['capacities']
    elif change == 'extra':
        output['invented'] = 1
    elif change == 'incomplete':
        output['problem']['costs'][0].pop()
    elif change == 'float':
        output['problem']['capacities'][0] = 2.0
    elif change == 'bool':
        output['problem']['capacities'][0] = True
    else:
        output = '{bad'
    result = simulated(source, output)
    assert result['outcome'] == 'abstain' and 'solver' not in result


def test_clarification_never_completed_by_invention():
    source, _ = fixture()
    result = simulated(source, dict(disposition='clarify', problem=None, issues=['missing value'], provenance={}))
    assert result['outcome'] == 'clarify' and result['candidate'] is None


@pytest.mark.parametrize('mode', ['source_only', 'locked_facts', 'deterministic'])
def test_faithful_optimum_and_independent_checker(mode):
    source, output = fixture()
    result = simulated(source, output, mode)
    assert result['outcome'] == 'verified' and result['optimality_proven']
    assert result['candidate']['objective'] == 1 and result['verification']['valid']
    assert result['llm_calls'] == (0 if mode == 'deterministic' else 1)


@pytest.mark.parametrize('mode', ['locked_facts', 'deterministic'])
def test_infeasibility(mode):
    source, output = fixture()
    source = source.replace('LoomA: 2 minute.', 'LoomA: 0 minute.').replace('LoomB: 3 minute.', 'LoomB: 0 minute.')
    output['problem']['capacities'] = [0, 0]
    output['provenance'] = {field: source for field in output['problem']}
    result = simulated(source, output, mode)
    assert result['outcome'] == 'infeasible' and result['solver']['raw_status'] == 'INFEASIBLE'


def test_partial_s1_does_not_claim_fidelity():
    source, output = fixture()
    source = 'Uninterpreted context.\n' + source
    output['provenance'] = {field: source for field in output['problem']}
    result = simulated(source, output)
    assert result['outcome'] == 'verified' and result['fidelity'] == 'unassessed'
    assert result['deterministic']['complete_problem'] is None


def test_s2_abstains_without_llm_on_unknown():
    result = d.run('Please allocate unknown things.', 'deterministic')
    assert result['outcome'] == 'abstain' and result['llm_calls'] == 0 and not result['attempts']


def test_reordering_labels_preserves_locks():
    source, output = fixture()
    problem = output['problem']
    problem['resources'].reverse()
    for field in ('costs', 'consumption', 'capacities'):
        problem[field].reverse()
    assert d.check_locks(source, problem)['accepted']


def test_prompt_only_intervention_and_unchanged_contract():
    source, _ = fixture()
    s0, s1 = d.request(source, 'source_only'), d.request(source, 'locked_facts')
    assert {k: v for k, v in s0.items() if k != 'messages'} == {k: v for k, v in s1.items() if k != 'messages'}
    assert s0['messages'][1] == s1['messages'][1] == dict(role='user', content=source)
    assert s0['messages'][0]['content'] == agent.INSTRUCTION
    assert s1['messages'][0]['content'].startswith(agent.INSTRUCTION + '\n' + d.HYBRID_INSTRUCTION)
    assert set(d.extract(source)) == {'version', 'source_sha256', 'declarations', 'established', 'facts',
        'unknown', 'contradictions', 'uninterpreted', 'constraints', 'coverage', 'complete_problem', 'complete_proof'}


def test_no_source_mutation_and_one_call():
    source, output = fixture()
    original = deepcopy(output)
    simulated(source, output)
    assert output == original


def test_source_contradiction_blocks_ready(monkeypatch):
    source, output = fixture()
    source += '\nCumulative capacity of LoomA: 4 minute.'
    output['provenance'] = {field: source for field in output['problem']}
    monkeypatch.setattr(agent, 'solve', lambda *args: pytest.fail('contradiction reached solver'))
    assert simulated(source, output)['reason'] == 'locked_fact_mismatch'


@pytest.mark.parametrize('mode', ['differential_proposals', '', None])
def test_unknown_mode_rejected(mode):
    with pytest.raises(ValueError):
        d.run('source', mode)


@pytest.mark.parametrize('value', [True, 1.0, '1'])
def test_fact_contract_strict_integer(value):
    source, _ = fixture()
    fact = deepcopy(d.extract(source)['facts'][0])
    fact['value'] = value
    with pytest.raises(ValueError):
        d.SourceFact.model_validate(fact)
