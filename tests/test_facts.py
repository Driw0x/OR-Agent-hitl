"""Deterministic E2-Facts controls. These are simulated outputs, not LLM results."""

from copy import deepcopy
import inspect
import json
import socket

import pytest

from or_agent import agent, crosscheck
from or_agent.crosscheck import (FactsExtraction, compare_facts, extract_facts,
                                facts_policy, facts_request, gap_facts, validate_facts)
from test_crosscheck import fixture as gap_fixture, reordered


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('deterministic tests must not access network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(crosscheck, 'ollama_chat', forbidden)


def fixture(prose=False):
    source, first = gap_fixture()
    if prose:
        source = 'A workshop requests this allocation.\n' + source
        first['provenance'] = {f: source for f in first['problem']}
    facts = []
    for (field, resource, task), (value, unit) in gap_facts(first['problem']).items():
        line = next(line for line in source.splitlines() if line.startswith(
            resource + (' capacity:' if field == 'capacities' else f' {field}:')))
        unit_line = 'Cost unit: credit.' if field == 'costs' else 'Resource unit: minute.'
        facts.append(dict(field=field, resource=resource, task=task, value=value, unit=unit,
                          evidence=[line, unit_line]))
    return source, first, dict(disposition='complete', tasks=['T1', 'T2'], resources=['R1', 'R2'],
                              facts=facts, uncertainties=[])


def test_complete_schema_and_associations():
    source, first, second = fixture()
    FactsExtraction.model_validate(second)
    report = compare_facts(first, second, source)
    assert report['status'] == 'agreement' and report['fidelity'] == 'unassessed'
    assert report['counts'] == dict(expected=10, extracted=10, structurally_valid=10,
        literally_justified=10, token_supported=10, association_verified=10,
        comparable=10, verified_comparable=10, match=10, mismatch=0, unknown=0)
    assert report['coverage'] == report['verified_coverage'] == 1


def test_permutations_and_unusual_declaration_order():
    source, first, second = fixture()
    lines = source.splitlines()
    source = '\n'.join([lines[0], *lines[1:][::-1]])
    first['provenance'] = {f: source for f in first['problem']}
    second['facts'].reverse()
    second['tasks'].reverse()
    second['resources'].reverse()
    report = compare_facts(reordered(first), second, source)
    assert report['status'] == 'agreement' and report['counts']['verified_comparable'] == 10


@pytest.mark.parametrize('field', ['costs', 'consumption', 'capacities', 'cost_unit', 'resource_unit'])
def test_incorrect_e1_value_or_unit(field):
    source, first, second = fixture()
    if field in ('costs', 'consumption'):
        first['problem'][field][0][0] += 1
    elif field == 'capacities':
        first['problem'][field][0] += 1
    else:
        first['problem'][field] = 'other'
    original = deepcopy(first)
    report = compare_facts(first, second, source)
    assert report['status'] == 'disagreement'
    assert report['differences'] and all(r['association'] == 'verified' for r in report['differences'])
    policy = facts_policy(True, report)
    assert policy['observation_admit'] and not policy['conservative_admit']
    assert first == original


@pytest.mark.parametrize('kind', ['transpose', 'row_association', 'task_association'])
def test_incorrect_e1_associations(kind):
    source, first, second = fixture()
    if kind == 'transpose':
        for field in ('costs', 'consumption'):
            first['problem'][field] = [list(row) for row in zip(*first['problem'][field])]
    elif kind == 'row_association':
        first['problem']['resources'].reverse()
    else:
        first['problem']['tasks'].reverse()
    report = compare_facts(first, second, source)
    assert report['counts']['mismatch'] > 0
    assert not facts_policy(True, report)['conservative_admit']


@pytest.mark.parametrize('kind', ['partial', 'missing_value', 'missing_unit', 'missing_key', 'bad_integer',
    'negative_amount', 'extra_field', 'bad_quote', 'concatenated_quote', 'missing_identifier',
    'missing_value_in_quote', 'missing_unit_in_quote'])
def test_partial_and_invalid_fact_salvage(kind):
    source, first, second = fixture()
    fact = second['facts'][0]
    if kind == 'partial':
        second['facts'].pop(0)
    elif kind == 'missing_value': fact['value'] = None
    elif kind == 'missing_unit': fact['unit'] = None
    elif kind == 'missing_key': del fact['resource']
    elif kind == 'bad_integer': fact['value'] = True
    elif kind == 'negative_amount': fact['value'] = -1
    elif kind == 'extra_field': fact['constraint'] = 'secret'
    elif kind == 'bad_quote': fact['evidence'] = ['not in source']
    elif kind == 'concatenated_quote': fact['evidence'] = ['R1 capacity: 5. Resource unit: minute.']
    elif kind == 'missing_identifier': fact['evidence'] = ['Resource unit: minute.']
    elif kind == 'missing_value_in_quote': fact['evidence'] = ['R1', 'Resource unit: minute.']
    elif kind == 'missing_unit_in_quote': fact['evidence'] = ['R1 capacity: 5.']
    report = compare_facts(first, second, source)
    assert report['status'] == 'partial' and report['counts']['comparable'] == 9
    assert report['counts']['unknown'] == 1 and report['coverage'] == .9
    assert facts_policy(True, report)['uncertain']


@pytest.mark.parametrize('kind', ['conflict', 'uncertain', 'duplicate', 'extra_fact', 'extra_identifier',
                                  'duplicate_identifier', 'false_complete'])
def test_uncertainty_duplicates_and_extras(kind):
    source, first, second = fixture()
    if kind == 'conflict':
        extra = deepcopy(second['facts'][0]); extra['value'] = 6
        second['facts'].append(extra)
    elif kind == 'uncertain':
        second['uncertainties'] = [dict(field='capacities', resource='R1', task=None,
                                        reason='two alternatives', evidence=[])]
    elif kind == 'duplicate': second['facts'].append(deepcopy(second['facts'][0]))
    elif kind == 'extra_fact':
        extra = deepcopy(second['facts'][0]); extra['resource'] = 'R3'
        second['facts'].append(extra)
    elif kind == 'extra_identifier': second['resources'].append('R3')
    elif kind == 'duplicate_identifier': second['resources'].append('R1')
    else: second['facts'] = second['facts'][:1]
    report = compare_facts(first, second, source)
    if kind == 'duplicate':
        assert report['status'] == 'agreement' and report['counts']['comparable'] == 10
    else:
        assert report['status'] != 'agreement'
    if kind in ('conflict', 'uncertain'):
        assert report['counts']['unknown'] == 1


def test_multiline_table_and_explicit_pairs_have_partial_proof():
    source, first, second = fixture(prose=True)
    source += '\nresource | task | cost | consumption\nR1 | T1 | 3 credit | 1 minute'
    first['provenance'] = {f: source for f in first['problem']}
    for fact in second['facts']:
        if fact['resource'] == 'R1' and fact['task'] == 'T1':
            fact['evidence'] = ['resource | task | cost | consumption\nR1 | T1 | 3 credit | 1 minute']
    report = compare_facts(first, second, source)
    assert report['counts']['comparable'] == 10
    assert report['counts']['verified_comparable'] == 0
    assert facts_policy(True, report)['uncertain']


@pytest.mark.parametrize('kind', ['misleading', 'both_wrong', 'correct_e1_wrong_e2', 'hypothetical', 'contradictory_source'])
def test_prose_propositions_never_authorize_blocking(kind):
    source, first, second = fixture(prose=True)
    fact = next(f for f in second['facts'] if f['field'] == 'costs' and f['resource'] == 'R1' and f['task'] == 'T1')
    if kind in ('misleading', 'both_wrong', 'correct_e1_wrong_e2'):
        fact['value'] = 9
        fact['evidence'] = [source]
        if kind == 'both_wrong': first['problem']['costs'][0][0] = 9
    elif kind == 'hypothetical':
        source = 'Hypothetical example; no authoritative problem selected.\n' + source
    else:
        source += '\nAlternative: R1 costs T1=9 credit; target scenario unspecified.'
        second['uncertainties'] = [dict(field='costs', resource='R1', task='T1',
                                        reason='ambiguous target', evidence=[])]
    first['provenance'] = {f: source for f in first['problem']}
    report = compare_facts(first, second, source)
    assert report['counts']['verified_comparable'] == 0
    assert facts_policy(True, report)['conservative_admit']
    assert report['fidelity'] == 'unassessed'
    if kind == 'both_wrong': assert report['status'] == 'agreement'
    if kind in ('misleading', 'correct_e1_wrong_e2'):
        assert report['status'] == 'disagreement'
        assert not facts_policy(True, report)['proposed_block_admit']
    if kind == 'contradictory_source': assert report['counts']['unknown'] == 1


def test_closed_source_contradicts_e2():
    source, first, second = fixture()
    fact = next(f for f in second['facts'] if f['field'] == 'costs' and f['task'] == 'T1' and f['resource'] == 'R1')
    fact['value'] = 9  # Appears in authentic evidence, but belongs to T2.
    report = compare_facts(first, second, source)
    assert report['counts']['unknown'] == 1 and report['status'] == 'partial'
    assert report['proposed_differences'][0]['association'] == 'contradicted'
    assert facts_policy(True, report)['conservative_admit']


@pytest.mark.parametrize('disposition', ['partial', 'unsupported', 'invalid'])
def test_noncomplete_and_nonready_e1(disposition):
    source, first, second = fixture()
    second['disposition'] = disposition
    assert compare_facts(first, second, source)['status'] == 'partial'
    first = dict(disposition='clarify', problem=None, issues=['missing information'], provenance={})
    report = compare_facts(first, second, source)
    assert report['status'] == 'inconclusive' and report['counts']['comparable'] == 0
    assert report['counts']['association_verified'] == 10
    assert report['counts']['unknown'] == report['counts']['expected'] == 10
    assert not facts_policy(False, report)['conservative_admit']


@pytest.mark.parametrize('bad', ['{', '{}', '[]', '{"facts":{},"facts":[]}'])
def test_unusable_envelope(bad):
    source, first, _ = fixture()
    report = compare_facts(first, bad, source)
    assert report['status'] == 'inconclusive' and report['counts']['unknown'] == 10


def test_source_only_prompt_and_no_solver(monkeypatch):
    source, first, second = fixture()
    sentinel = 'REFERENCE_E1_SOLVER_SECRET_598327'
    first['problem']['cost_unit'] = sentinel
    def client(request, timeout):
        assert sentinel not in json.dumps(request)
        assert request == facts_request(source) and 0 < timeout <= 180
        return dict(done=True, message=dict(content=json.dumps(second)))
    monkeypatch.setattr(agent, 'solve', lambda *args: pytest.fail('solver forbidden'))
    result = extract_facts(source, client=client, admission=lambda: dict(allowed=True))
    assert result['calls'] == 1 and result['validation']['schema_valid']
    assert list(inspect.signature(facts_request).parameters) == ['source']
    assert not {'first', 'reference', 'solver', 'labels'} & set(inspect.signature(extract_facts).parameters)
    assert facts_request(source)['options'] == agent.OPTIONS
    assert len(facts_request(source)['messages']) == 2


@pytest.mark.parametrize('kind', ['transport', 'truncated', 'not_done', 'length', 'missing_content', 'schema_invalid'])
def test_one_call_failure_no_retry(kind):
    source, _, second = fixture()
    def client(*args):
        if kind == 'transport': raise ConnectionError('simulated failure')
        if kind == 'missing_content': return dict(done=True)
        return dict(done=kind != 'not_done', done_reason='length' if kind == 'length' else 'stop',
            message=dict(content='{' if kind in ('truncated', 'length') else '{}' if kind == 'schema_invalid' else json.dumps(second)))
    result = extract_facts(source, client=client, admission=lambda: dict(allowed=True))
    assert result['calls'] == 1
    assert result['termination_uncertain'] == (kind in ('transport', 'not_done'))


@pytest.mark.parametrize('kwargs', [dict(max_calls=2), dict(max_calls=True), dict(total_seconds=181),
                                  dict(total_seconds=float('nan')), dict(total_seconds=True)])
def test_invalid_budgets(kwargs):
    with pytest.raises(ValueError): extract_facts(fixture()[0], **kwargs)


@pytest.mark.parametrize('kind', ['zero_calls', 'zero_time', 'refused', 'elapsed_admission'])
def test_no_call_without_admission_or_budget(kind, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(crosscheck.time, 'monotonic', lambda: now[0])
    def admission():
        if kind == 'elapsed_admission': now[0] = 180
        return dict(allowed=kind != 'refused', reason='refused')
    result = extract_facts(fixture()[0], admission=admission,
        max_calls=0 if kind == 'zero_calls' else 1, total_seconds=0 if kind == 'zero_time' else 180)
    assert result['calls'] == 0


def test_out_of_scope_and_missing_fields_do_not_gain_source_proof():
    source, first, second = fixture()
    source += '\nT1 must finish before noon; do not discard this deadline.'
    first['provenance'] = {f: source for f in first['problem']}
    second['disposition'] = 'unsupported'
    report = compare_facts(first, second, source)
    assert report['status'] == 'partial' and report['counts']['verified_comparable'] == 0
    assert facts_policy(True, report)['uncertain']


def test_missing_identifier_inventory_and_whole_source_quotes():
    source, first, second = fixture()
    second['tasks'].pop()
    for fact in second['facts']: fact['evidence'] = [source]
    report = compare_facts(first, second, source)
    assert report['status'] == 'partial'
    assert report['inventory']['tasks']['missing'] == ['T2']
    assert report['counts']['association_verified'] == 0
