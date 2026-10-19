"""Controlled E1/E2 pairs, never generated Ollama observations."""

from copy import deepcopy
import inspect
import json

import pytest

from or_agent import agent, crosscheck
from or_agent.crosscheck import compare, conservative_policy, control_request, extract_control
from or_agent.models import Gap


def fixture():
    source = ('Assign each task exactly once; minimize total cost within capacities.\n'
              'Tasks: T1, T2.\nResources: R1, R2.\n'
              'R1 costs: T1=3, T2=9.\nR2 costs: T1=7, T2=2.\n'
              'R1 consumption: T1=1, T2=4.\nR2 consumption: T1=3, T2=2.\n'
              'R1 capacity: 5.\nR2 capacity: 6.\nCost unit: credit.\nResource unit: minute.')
    problem = dict(tasks=['T1', 'T2'], resources=['R1', 'R2'], costs=[[3, 9], [7, 2]],
                   consumption=[[1, 4], [3, 2]], capacities=[5, 6],
                   cost_unit='credit', resource_unit='minute')
    return source, dict(disposition='ready', problem=problem, issues=[],
                        provenance={field: source for field in problem})


def reordered(extraction):
    result = deepcopy(extraction)
    p = result['problem']
    for field in ('tasks', 'resources', 'capacities'):
        p[field].reverse()
    for field in ('costs', 'consumption'):
        p[field] = [row[::-1] for row in p[field][::-1]]
    return result


@pytest.mark.parametrize('kind', ['identical', 'reordered', 'both_wrong', 'same_numbers_different_quotes'])
def test_agreement(kind):
    source, first = fixture()
    if kind == 'both_wrong':
        first['problem']['consumption'][0][0] = 999
    second = reordered(first) if kind == 'reordered' else deepcopy(first)
    if kind == 'same_numbers_different_quotes':
        second['provenance'] = {f: 'T1' for f in Gap.model_fields}
    original = deepcopy((first, second))
    report = compare(first, second, source)
    assert report['status'] == 'agreement'
    assert report['fidelity'] == 'unassessed'
    assert not report['differences']
    assert (first, second) == original


@pytest.mark.parametrize('field', ['costs', 'consumption', 'capacities', 'cost_unit', 'resource_unit',
                                  'missing_task', 'missing_resource', 'transpose', 'row_association', 'constraint'])
def test_disagreement(field):
    source, first = fixture()
    second = deepcopy(first)
    p = second['problem']
    expected = field
    if field in ('costs', 'consumption'):
        p[field][0][0] += 1
    elif field == 'capacities':
        p[field][0] += 1
    elif field in ('cost_unit', 'resource_unit'):
        p[field] = 'other'
    elif field == 'missing_task':
        p['tasks'].pop()
        for f in ('costs', 'consumption'):
            for row in p[f]:
                row.pop()
        expected = 'tasks'
    elif field == 'missing_resource':
        for f in ('resources', 'costs', 'consumption', 'capacities'):
            p[f].pop()
        expected = 'resources'
    elif field == 'transpose':
        p['costs'] = [list(row) for row in zip(*p['costs'])]
        expected = 'costs'
    elif field == 'row_association':
        p['resources'].reverse()
        expected = 'consumption'
    elif field == 'constraint':
        # An incorrectly encoded capacity constraint changes its coefficient.
        p['consumption'][0][0] = 0
        expected = 'consumption'
    original = deepcopy((first, second))
    report = compare(first, second, source)
    assert report['status'] == 'disagreement'
    assert expected in {d['field'] for d in report['differences']}
    assert report['fidelity'] == 'unassessed'
    assert (first, second) == original
    if field in ('costs', 'consumption'):
        assert report['differences'] == [dict(field=field, E1=first['problem'][field][0][0],
            E2=p[field][0][0], resource='R1', task='T1')]


@pytest.mark.parametrize('kind', ['invalid_E1', 'invalid_E2', 'clarify', 'missing_data', 'unsupported',
                                 'invalid_disposition', 'truncated', 'no_candidate', 'bad_quote',
                                 'extra_constraint', 'extra_objective', 'rectangular_transpose'])
def test_inconclusive(kind):
    source, first = fixture()
    second = deepcopy(first)
    side = 'E2'
    if kind == 'invalid_E1':
        first = None
        side = 'E1'
    elif kind in ('invalid_E2', 'no_candidate'):
        second = None
    elif kind in ('clarify', 'unsupported', 'invalid_disposition'):
        disposition = 'invalid' if kind == 'invalid_disposition' else kind
        second = dict(disposition=disposition, problem=None, issues=['source uncertain'], provenance={})
    elif kind == 'missing_data':
        del second['problem']['capacities']
    elif kind == 'truncated':
        second = '{"disposition":'
    elif kind == 'bad_quote':
        second['provenance']['costs'] = 'absent'
    elif kind in ('extra_constraint', 'extra_objective'):
        second['problem']['constraints' if kind == 'extra_constraint' else 'objective'] = 'different'
    elif kind == 'rectangular_transpose':
        second['problem']['tasks'].append('T3')
    report = compare(first, second, source)
    assert report['status'] == 'inconclusive'
    assert report['reasons'][0]['extraction'] == side
    assert not report['differences']


def test_p1_and_mutated_model_revalidated():
    source, first = fixture()
    multi = deepcopy(first)
    multi['provenance'] = {f: [q] for f, q in multi['provenance'].items()}
    assert compare(first, multi, source)['status'] == 'agreement'
    model = crosscheck.validated(first, source)
    model.problem.costs[0].pop()
    assert compare(model, first, source)['status'] == 'inconclusive'


def test_no_reference_e1_solver_or_labels_in_request(monkeypatch):
    source, first = fixture()
    sentinel = 'SECRET_REFERENCE_E1_SOLVER_LABEL_9481'
    first['problem']['cost_unit'] = sentinel
    seen = []
    def client(payload, timeout):
        seen.append(deepcopy(payload))
        assert 0 < timeout <= 180
        assert sentinel not in json.dumps(payload)
        return dict(done=True, message=dict(content=json.dumps(fixture()[1])))
    monkeypatch.setattr(agent, 'solve', lambda *a: pytest.fail('E2 invokes solver'))
    result = extract_control(source, client=client, admission=lambda: dict(allowed=True))
    assert len(seen) == result['calls'] == 1
    assert seen[0] == control_request(source)
    assert seen[0]['messages'][1] == dict(role='user', content=source)
    assert len(seen[0]['messages']) == 2
    assert seen[0]['messages'][0]['content'] != agent.INSTRUCTION
    assert seen[0]['format'] == crosscheck.Extraction.model_json_schema()
    assert not {'first', 'reference', 'solver', 'case'} & set(inspect.signature(extract_control).parameters)
    assert result['json_valid'] and result['schema_valid'] and result['provenance_valid']
    assert first['problem']['cost_unit'] == sentinel


@pytest.mark.parametrize('kind', ['transport', 'truncated', 'missing', 'bad_quote', 'not_done', 'length'])
def test_single_attempt_failures(kind):
    source, first = fixture()
    def client(payload, timeout):
        if kind == 'transport':
            raise ConnectionError('controlled transport failure')
        if kind == 'missing':
            return dict(done=True)
        if kind == 'bad_quote':
            first['provenance']['costs'] = 'absent'
        return dict(done=kind != 'not_done', done_reason='length' if kind == 'length' else 'stop',
                    message=dict(content='{' if kind in ('truncated', 'length') else json.dumps(first)))
    result = extract_control(source, client=client, admission=lambda: dict(allowed=True))
    assert result['calls'] == 1 and result['extraction'] is None
    assert compare(first, result['extraction'], source)['status'] == 'inconclusive'
    assert result['termination_uncertain'] == (kind in ('transport', 'not_done'))


@pytest.mark.parametrize('kind', ['zero_calls', 'zero_time', 'refused', 'admission_consumes_budget'])
def test_budgets(kind, monkeypatch):
    source, _ = fixture()
    now = [0.0]
    monkeypatch.setattr(crosscheck.time, 'monotonic', lambda: now[0])
    def admission():
        if kind == 'admission_consumes_budget':
            now[0] = 180
        return dict(allowed=kind != 'refused', reason='controlled_resource_refusal')
    result = extract_control(source, max_calls=0 if kind == 'zero_calls' else 1,
        total_seconds=0 if kind == 'zero_time' else 180, admission=admission,
        client=lambda *a: pytest.fail('unexpected call'))
    assert result['calls'] == 0
    assert result['extraction'] is None


@pytest.mark.parametrize('options', [dict(max_calls=2), dict(max_calls=True),
                                  dict(total_seconds=181), dict(total_seconds=float('nan'))])
def test_invalid_budgets(options):
    with pytest.raises(ValueError):
        extract_control(fixture()[0], **options)


@pytest.mark.parametrize('status', ['agreement', 'disagreement', 'inconclusive'])
@pytest.mark.parametrize('admitted', [True, False])
def test_policy(status, admitted):
    result = conservative_policy(admitted, dict(status=status))
    assert result == dict(admit=admitted and status != 'disagreement',
        blocked=admitted and status == 'disagreement', uncertain=status == 'inconclusive', fidelity='unassessed')


def test_default_v1_and_locks_unchanged():
    source, first = fixture()
    # V1 still admits wrong but structurally valid data and does not consult E2.
    first['problem']['costs'][0][0] = 99
    result = agent.run(source, method='structured', client=lambda *a: dict(message=dict(content=json.dumps(first))),
                       admission=lambda: dict(allowed=True))
    assert len(result['attempts']) == 1
    assert result['fidelity'] == 'unassessed'
    assert 'experimental' not in result
    changed = deepcopy(first['problem'])
    changed['costs'][0][0] = 3
    assert not agent.preserved(first['problem'], changed)
    assert 'crosscheck' not in inspect.getsource(agent.run)
