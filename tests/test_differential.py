"""Offline differential_proposals contract, source-only prompts, semantic gates and real CP-SAT."""

from copy import deepcopy
import json
import socket

import pytest

from or_agent import agent, deterministic as d, differential as s3
from or_agent.models import Gap


def fixture():
    source = ('Assign each task exactly once; minimize total cost within capacities.\n'
        'Tasks: Cut, Seal.\nResources: Press, Bench.\nCost unit: credit.\nResource unit: minute.\n'
        'Press costs: Cut=-2, Seal=4.\nBench costs: Cut=6, Seal=1.\n'
        'Press capacity: 4.\nBench capacity: 3.\nPress consumption: Cut=2, Seal=3.\n'
        'Measurement: Bench consumption: Cut=4, Seal=2.')
    delta = dict(proposals=[dict(field='consumption',resource='Bench',task=t,value=v,unit='minute',
        evidence='Measurement: Bench consumption: Cut=4, Seal=2.',uncertainty='certain',clarification=None)
        for t,v in [('Cut',4),('Seal',2)]],unresolved=[])
    return source, delta


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('offline test attempted network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


def simulated(source, delta):
    calls = []
    def client(payload, timeout):
        calls.append(payload)
        return dict(message=dict(content=delta if isinstance(delta,str) else json.dumps(delta)))
    result = s3.run(source,client=client,admission=lambda:dict(allowed=True))
    assert len(calls)==result['llm_calls']<=1
    return result


def test_verified_merge_optimum_and_input_immutability():
    source, delta = fixture()
    before = deepcopy(delta)
    merged = s3.merge(source,delta)
    assert merged['outcome']=='ready' and merged['locked_preserved']
    assert merged['locked_count']==8 and len(merged['accepted'])==2 and not merged['missing']
    assert merged['problem']['consumption']==[[2,3],[4,2]]
    assert delta==before
    result = simulated(source,delta)
    assert result['outcome']=='verified' and result['candidate']['objective']==-1
    assert result['optimality_proven'] and result['verification']['valid']
    assert result['fidelity']=='unassessed' and result['explanation_valid']


@pytest.mark.parametrize('change', ['field','resource','task','negative','overflow','bool','float','unit','blank','matrix','unknown_key','uncertainty','null_task','duplicate_json'])
def test_contract_and_semantic_rejections(change):
    source, delta = fixture()
    p = delta['proposals'][0]
    if change=='field': p['field']='objective'
    elif change=='resource': p['resource']='Other'
    elif change=='task': p['task']='Other'
    elif change=='negative': p['value']=-1
    elif change=='overflow': p['value']=1000000001
    elif change=='bool': p['value']=True
    elif change=='float': p['value']=4.0
    elif change=='unit': p['unit']='hour'
    elif change=='blank': p['evidence']=' '
    elif change=='matrix': delta['consumption']=[[2,3],[4,2]]
    elif change=='unknown_key': p['verified']=True
    elif change=='uncertainty': p['uncertainty']='uncertain'
    elif change=='null_task': p['task']=None
    else: delta='{"proposals":[],"proposals":[],"unresolved":[]}'
    result=simulated(source,delta)
    assert result['outcome']=='abstain' and 'solver' not in result


@pytest.mark.parametrize('field,resource,task,value,unit', [
    ('costs','Press','Cut',9,'credit'), ('consumption','Press','Cut',9,'minute'),
    ('capacities','Press',None,9,'minute'), ('consumption','Press','Cut',2,'minute')])
def test_locked_targets_never_replaced(field,resource,task,value,unit,monkeypatch):
    source,delta=fixture()
    delta['proposals'].append(dict(field=field,resource=resource,task=task,value=value,unit=unit,
        evidence=source,uncertainty='certain',clarification=None))
    monkeypatch.setattr(agent,'solve',lambda *a:pytest.fail('lock violation reached solver'))
    result=simulated(source,delta)
    assert result['merge']['locked_preserved'] and result['outcome']=='abstain'
    assert result['merge']['rejected'][-1]['reason']=='locked_target'


@pytest.mark.parametrize('change', ['quote','wrong_value','invention','contradiction','transpose','uncertain','partial'])
def test_proposal_diagnostics(change):
    source,delta=fixture()
    p=delta['proposals'][0]
    if change=='quote': p['evidence']='nonexistent quote'
    elif change=='wrong_value': p['value']=2
    elif change=='invention': p['value']=999
    elif change=='contradiction': delta['proposals'].append(dict(p,value=2))
    elif change=='transpose': p['resource'],p['task']='Cut','Bench'
    elif change=='uncertain': p.update(uncertainty='uncertain',clarification='Confirm Bench/Cut load.')
    else: delta['proposals'].pop()
    merged=s3.merge(source,delta)
    assert merged['problem'] is None and merged['locked_preserved']
    assert merged['outcome']==('clarify' if change in ('uncertain','partial') else 'abstain')
    if change=='partial': assert len(merged['accepted'])==1 and len(merged['missing'])==1
    if change=='uncertain':
        assert merged['unconfirmed'][0]['source_sha256']==d.extract(source)['source_sha256']
        assert merged['unconfirmed'][0]['proposal_sha256']


@pytest.mark.parametrize('suffix', ['Extra: assign Cut to Press only.', 'Historical values are obsolete.', ' ', 'Unspecified constraint applies.'])
def test_unknown_global_semantics_prevent_confirmation(suffix,monkeypatch):
    source,delta=fixture()
    monkeypatch.setattr(agent,'solve',lambda *a:pytest.fail('unproven semantics reached solver'))
    result=simulated(source+'\n'+suffix,delta)
    assert result['outcome']=='abstain' and len(result['merge']['unconfirmed'])==2
    assert not result['merge']['accepted']


def test_authentic_prose_quote_never_proves_semantics():
    source,delta=fixture()
    prose='At Bench, Cut consumes 4 minute and Seal consumes 2 minute.'
    source=source.replace(delta['proposals'][0]['evidence'],prose)
    for p in delta['proposals']:p['evidence']=prose
    merged=s3.merge(source,delta)
    assert len(merged['unconfirmed'])==2 and merged['problem'] is None


def test_incomplete_and_appropriate_clarification():
    source,delta=fixture()
    source=source.replace('Measurement: Bench consumption: Cut=4, Seal=2.','')
    delta=dict(proposals=[],unresolved=[dict(field='consumption',resource='Bench',task=None,
        evidence=None,reason='Please supply both Bench consumption values.')])
    merged=s3.merge(source,delta)
    assert merged['outcome']=='clarify' and len(merged['missing'])==2 and merged['unresolved']


def test_verifiable_addition_does_not_fill_another_missing_value():
    source,delta=fixture()
    source=source.replace('Press consumption: Cut=2, Seal=3.','Press consumption: Cut=2.')
    merged=s3.merge(source,delta)
    assert len(merged['accepted'])==2 and merged['outcome']=='clarify'
    assert merged['missing']==[['consumption','Press','Seal']]


def test_source_contradiction_never_solved():
    source,delta=fixture()
    result=simulated(source+'\nPress capacity: 999.',delta)
    assert result['outcome']=='invalid' and 'solver' not in result


def test_infeasible_reference_is_not_a_merge_error():
    source,delta=fixture()
    source=source.replace('Press capacity: 4.','Press capacity: 0.').replace('Bench capacity: 3.','Bench capacity: 0.')
    result=simulated(source,delta)
    assert result['outcome']=='infeasible' and result['solver']['raw_status']=='INFEASIBLE'
    assert result['candidate'] is None and not result['optimality_proven']


def test_s2_shortcut_calls_no_client_or_admission():
    source,delta=fixture()
    source=source.replace('Measurement: ','')
    result=s3.run(source,client=lambda *a:pytest.fail('complete deterministic called LLM'),
        admission=lambda:pytest.fail('deterministic attempted LLM admission'))
    assert result['route']=='deterministic_direct' and result['llm_calls']==0 and result['optimality_proven']


def test_no_facts_is_explicit_and_calls_no_llm():
    result=s3.run('Allocate jobs somehow.',client=lambda *a:pytest.fail('no useful locks'))
    assert result['outcome']=='abstain' and result['llm_calls']==0 and result['reason']=='no_useful_deterministic_facts'


def test_missing_unit_never_converted():
    source,delta=fixture()
    source=source.replace('Resource unit: minute.','Loads are in minutes.')
    merged=s3.merge(source,delta)
    assert merged['problem'] is None and len(merged['unconfirmed'])==2


def test_prompt_boundary_only_unknown_passages_and_locked_tuples():
    source,delta=fixture()
    request=s3.request(source)
    view=json.loads(request['messages'][1]['content'])
    assert set(view)=={'locked','declarations','unknown','contradictions','standard_gap_proven','passages'}
    assert view['passages']==d.extract(source)['uninterpreted']
    assert len(view['locked'])==8 and view['unknown'][0]['missing']==[['consumption','Bench','Cut'],['consumption','Bench','Seal']]
    assert source not in request['messages'][1]['content']
    assert request['options']==agent.OPTIONS and request['format']==s3.Differential.model_json_schema()
    assert not any(word in json.dumps(request) for word in ('reference_optimum','reference_feasible_count','strict_fidelity','candidate'))
    with pytest.raises((TypeError,ValueError)):
        s3.request(dict(source=source,reference_optimum=-1))


def test_empty_and_malformed_differentials_abstain():
    source,_=fixture()
    assert simulated(source,dict(proposals=[],unresolved=[]))['outcome']=='clarify'
    assert simulated(source,'{bad')['outcome']=='abstain'


def test_resource_refusal_and_uncertain_transport():
    source,_=fixture()
    result=s3.run(source,admission=lambda:dict(allowed=False,reason='ram'),client=lambda *a:pytest.fail('refused'))
    assert result['outcome']=='resource_blocked' and result['llm_calls']==0
    def fail(*a):raise TimeoutError('uncertain')
    result=s3.run(source,admission=lambda:dict(allowed=True),client=fail)
    assert result['outcome']=='execution_error' and result['llm_calls']==1 and 'solver' not in result


def test_empty_confirmation_reason_rejected():
    with pytest.raises(ValueError):
        s3.Unresolved(field='constraints',resource=None,task=None,evidence=None,reason=' ')
