import copy
from pathlib import Path

import pytest

from or_agent.models import Gap, load_json, read_gap
from or_agent.solver import solve
from or_agent.verify import verify


def inputs():
    return read_gap(Path('examples/small.json').read_text()), load_json(Path('examples/candidate.json').read_text())


def test_correct():
    p,c = inputs()
    r = verify(p,c)
    assert r['valid'] and r['recomputed_objective'] == 6
    assert r['loads'] == {'R1':4,'R2':2}
    assert verify(p,solve(p)['candidate'])['valid']


@pytest.mark.parametrize('fault,kind', [
    ('missing','task_coverage'), ('duplicate','task_coverage'),
    ('unknown_task','unknown_task'), ('unknown_resource','unknown_resource'),
    ('capacity','capacity'), ('objective','objective'), ('extra','candidate_structure')])
def test_injections(fault,kind):
    p,c = inputs()
    if fault == 'missing': c['assignments'].pop()
    if fault == 'duplicate': c['assignments'].append(copy.deepcopy(c['assignments'][0]))
    if fault == 'unknown_task': c['assignments'][0]['task'] = 'X'
    if fault == 'unknown_resource': c['assignments'][0]['resource'] = 'X'
    if fault == 'capacity': c['assignments'][1]['resource'] = 'R1'
    if fault == 'objective': c['objective'] = 0
    if fault == 'extra': c['claims_optimal'] = True
    r = verify(p,c)
    assert not r['valid']
    assert kind in {v['kind'] for v in r['violations']}


def test_valid_wrong_translation_is_not_source_fidelity():
    p,c = inputs()
    data = p.model_dump()
    data['costs'][0][0] = 1
    wrong = Gap.model_validate(data)
    c['objective'] = 5
    assert verify(wrong,c)['valid']
    assert not verify(p,c)['valid']
