"""Offline real HTTP/SSE and CP-SAT; every test approval is simulated."""

from http.client import HTTPConnection
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import pytest

from or_agent import hitl, routing, visualization, web

@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(hitl.agent, 'ollama_chat', lambda *a: pytest.fail('offline web attempted Ollama'))
    # A local import fixture keeps tests independent of optional pilot traces.
    original = tmp_path/'historical/session'
    routing.submit(original, web.PROSE.encode('utf-8'), human_scope=True)
    hitl.write(original/'proposal.txt', hitl.FORMAT.encode('utf-8'))
    hitl.write(original/'llm-raw.txt', hitl.FORMAT.encode('utf-8'))
    hitl.validate(original)
    store = web.Store(tmp_path / 'sessions', simulated_reviewer=True, imports=[original])
    server = web.make_server(store, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield store, server
    store.close()
    server.shutdown()
    server.server_close()
    thread.join(5)


def request(app, path, body=None, *, headers=None, method=None):
    _, server = app
    conn = HTTPConnection('127.0.0.1', server.server_port, timeout=10)
    selected = {'Origin':f'http://127.0.0.1:{server.server_port}', 'X-OR-Agent':'1','Content-Type':'application/json'}
    if headers is not None:
        selected.update(headers)
    conn.request(method or ('GET' if body is None else 'POST'), path,
                 None if body is None else json.dumps(body), selected)
    response = conn.getresponse()
    content = response.read()
    status = response.status
    mime = response.getheader('Content-Type')
    conn.close()
    return status, load(content) if 'application/json' in mime else content.decode('utf-8')


def load(data):
    return json.loads(data)


def idle(app, sid):
    deadline = time.monotonic()+15
    while time.monotonic()<deadline:
        state = app[0].read(sid)
        if not state['active']:
            return state
        time.sleep(.01)
    pytest.fail('web job did not finish')


def create(app, source=web.EXAMPLE):
    code, out = request(app, '/api/sessions', dict(source=source))
    assert code == 202, out
    return out['id'], idle(app, out['id'])


def action(app, sid, name, body=None):
    code, out = request(app,f'/api/sessions/{sid}/{name}',body or {})
    assert code == 202, out
    return idle(app,sid)


def proposal(app, sid, value=hitl.FORMAT):
    state = app[0].read(sid)['snapshot']['hitl']
    return action(app,sid,'edit',dict(proposal=value,proposal_sha256=state['fingerprints']['proposal_sha256']))


def approve(app, sid):
    data = action(app,sid,'review')
    token=data['snapshot']['hitl']['review_token']
    data=action(app,sid,'scope',dict(review_token=token,confirmation='SCOPE:'+token))
    assert not data['error'], data['error']
    data=action(app,sid,'approve',dict(review_token=token,confirmation='APPROVE:'+token))
    assert data['snapshot']['hitl']['approval']['actor']=='simulated_reviewer'
    return data


def ready_to_resolve(app, sid):
    data = action(app, sid, 'review')
    token = data['snapshot']['hitl']['review_token']
    data = action(app, sid, 'scope', dict(review_token=token, confirmation='SCOPE:'+token))
    assert not data['error']
    return dict(review_token=token, confirmation='APPROVE:'+token)


def test_home_assets_examples_and_security_headers(app):
    code, page=request(app,'/')
    assert code==200 and 'Votre problème' in page and 'id="proposal"' in page
    for path in ('/app.js','/style.css'):
        assert request(app,path)[0]==200
    assert request(app,'/api/examples')[1]['autonomous']==web.EXAMPLE
    assert request(app,'/../or_agent/cli.py')[0]==404
    assert request(app,'/',headers={'Host':'attacker.test'})[0]==400


@pytest.mark.parametrize('source',[None,'','  ','x'*8001,'x\x00',42,'\ud800'])
def test_reject_bad_source(app,source):
    assert request(app,'/api/sessions',dict(source=source))[0]==400
    assert not app[0].listing()['sessions']


@pytest.mark.parametrize('headers',[
    {'Origin':'https://evil.test'}, {'Origin':''}, {'X-OR-Agent':''},
    {'Content-Type':'text/plain'}, {'Host':'evil.test'}])
def test_mutation_origin_and_content_type(app,headers):
    assert request(app,'/api/sessions',dict(source=web.EXAMPLE),headers=headers)[0] in (400,403)
    assert not app[0].listing()['sessions']


def test_json_duplicate_keys_length_and_malformed(app):
    _,server=app
    for content in ('{"source":"a","source":"b"}', '{', '[]'):
        conn=HTTPConnection('127.0.0.1',server.server_port)
        conn.request('POST','/api/sessions',content,{'Origin':f'http://127.0.0.1:{server.server_port}','X-OR-Agent':'1','Content-Type':'application/json'})
        assert conn.getresponse().status==400
        conn.close()
    assert request(app,'/api/sessions',dict(source='a'*140000))[0]==400


def test_autonomous_minus_five_no_ollama_events_and_exports(app,monkeypatch):
    monkeypatch.setattr(web.resources,'snapshot',lambda *a,**k:pytest.fail('deterministic inspected Ollama'))
    sid,data=create(app)
    assert not data['error'], data['error']
    state=data['snapshot'];result=state['result']
    assert state['routing']['branch']=='AUTONOMOUS'
    assert result['llm_calls']==0 and result['solver']['objective']==-5
    assert result['verification']['loads']=={'R1':3,'R10':0}
    assert result['optimality_proven'] and result['verification']['valid']
    assert not (app[0].path(sid)/'session/generation.json').exists()
    events=data['events']
    assert [e['id'] for e in events]==list(range(1,len(events)+1))
    completions=[e['stage'] for e in events if e['status']=='completed']
    assert completions==['received','extraction','data','constraints','certificate','routing','conversion','solver','verification','graphics']
    assert all('duration_seconds' in e for e in events if e['status']=='completed' and e['stage']!='received')
    for kind,needle in [('graph.svg','<svg'),('result.html','Utilisation des ressources'),('result.json','solver')]:
        code,body=request(app,f'/api/sessions/{sid}/{kind}');assert code==200 and needle in body
    monkeypatch.setattr(hitl,'solve_gap',lambda *a,**k:pytest.fail('repeat solve'))
    assert request(app,f'/api/sessions/{sid}')[1]['snapshot']['result']==result
    assert action(app,sid,'solve')['snapshot']['result']==result


def test_sse_replays_in_order_and_resumes_without_actions(app):
    sid,data=create(app)
    _,server=app
    def stream(after,count):
        conn=HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        conn.request('GET',f'/api/sessions/{sid}/events',headers={'Last-Event-ID':str(after)})
        response=conn.getresponse();assert response.status==200
        rows=[]
        while len(rows)<count:
            line=response.readline().decode()
            if line.startswith('data: '):rows.append(load(line[6:]))
        conn.close();return rows
    rows=stream(0,len(data['events']))
    assert rows==data['events']
    assert stream(len(rows)-1,1)==rows[-1:]
    assert app[0].read(sid)['events']==data['events']
    assert request(app,f'/api/sessions/{sid}/events?after=bad')[0]==400


def test_free_prose_import_simulated_scope_optimum_twelve_and_stale_edit(app):
    OLD = app[0].imports['0']
    old={p.name:hitl.sha(p.read_bytes()) for p in OLD.iterdir() if p.is_file()}
    code,out=request(app,'/api/import',dict(id='0'));assert code==202
    sid=out['id'];data=idle(app,sid)
    assert not data['error'],data['error']
    state=data['snapshot']
    assert state['routing']['branch']=='HITL' and state['hitl']['validation']['complete']
    assert state['raw']==(OLD/'llm-raw.txt').read_text(encoding='utf-8')
    assert not state['result'] and not state['hitl']['approval_valid']
    assert action(app,sid,'solve')['error']
    data=action(app,sid,'review');token=data['snapshot']['hitl']['review_token']
    assert action(app,sid,'approve',dict(review_token=token,confirmation='APPROVE:'+token))['error']
    approve(app,sid)
    data=action(app,sid,'solve');assert not data['error'],data['error']
    result=data['snapshot']['result']
    assert result['solver']['objective']==12 and result['optimality_proven']
    assert result['verification']['loads']=={'Machine_A':3,'Machine_B':3}
    data=proposal(app,sid,state['proposal'].replace('cost=8 euro','cost=9 euro'))
    assert not data['snapshot']['hitl']['approval_valid']
    assert data['snapshot']['result_current'] is False
    assert data['snapshot']['result']==result
    for kind in ('graph.svg','result.html','result.json'):
        assert request(app,f'/api/sessions/{sid}/{kind}')[0]==409
    assert action(app,sid,'solve')['error']
    assert {p.name:hitl.sha(p.read_bytes()) for p in OLD.iterdir() if p.is_file()}==old


@pytest.mark.parametrize('clause',[
    'La tâche Cut doit précéder Assemble.',
    'Cut doit être exécutée avant Assemble.', 'Cut must run before Assemble.',
    'Une échéance doit être respectée.', 'The tasks have a deadline.',
    'Use Machine_A first.', 'This is a routing problem.',
    'Cut must use Machine_A.', 'Compute a vehicle route visiting every customer.',
    'If Cut uses Machine_A, Assemble must use Machine_B.', 'Correction: Cut can be omitted.',
    'Every task must be assigned twice and exactly once.', 'Machine_A cannot perform Cut.',
    'Either omit Cut or assign Assemble to both resources.',
    'For Machine_A only, interpret costs as consumptions and consumptions as costs.'])
def test_detected_extra_constraints_cannot_be_deleted_or_confirmed(app,clause):
    sid,data=create(app,web.PROSE+'\n'+clause)
    proposal(app,sid)
    data=action(app,sid,'review')
    assert data['snapshot']['hitl']['validation']['source_blockers']
    token=data['snapshot']['hitl']['review_token']
    assert action(app,sid,'scope',dict(review_token=token,confirmation='SCOPE:'+token))['error']
    assert action(app,sid,'approve',dict(review_token=token,confirmation='APPROVE:'+token))['error']
    assert action(app,sid,'solve')['snapshot']['result'] is None


def test_original_contradiction_persists(app):
    sid,data=create(app,web.EXAMPLE+'\nR1 capacity: 3.')
    proposal(app,sid)
    assert action(app,sid,'review')['snapshot']['hitl']['validation']['source_blockers']
    assert action(app,sid,'solve')['error']


def test_missing_invalid_proposal_cannot_approve_and_rejection_terminal(app):
    sid,data=create(app,web.PROSE)
    proposal(app,sid,hitl.FORMAT.replace('cost=8 euro','cost=? euro'))
    data=action(app,sid,'review')
    assert data['snapshot']['hitl']['validation']['missing']
    assert not data['snapshot']['hitl']['validation']['complete']
    data=proposal(app,sid,'not HITL');assert data['snapshot']['hitl']['validation']['errors']
    proposal(app,sid)
    data=action(app,sid,'reject',dict(reason='test simulated rejection'))
    assert data['snapshot']['hitl']['state']=='REJECTED'
    assert action(app,sid,'solve')['error']
    assert proposal(app,sid)['error']


def test_llm_actual_raw_controlled_events_reconnect_and_one_call(app):
    started,finish=threading.Event(),threading.Event()
    calls=[]
    expected_raw=hitl.FORMAT.replace('\n','\r\n')
    def client(payload,timeout):
        calls.append(payload);started.set();assert finish.wait(10)
        return dict(done=True,message=dict(content=expected_raw,thinking='private reasoning fixture'))
    app[0].client=client;app[0].admission=lambda:dict(allowed=True)
    sid,_=create(app,web.PROSE)
    assert request(app,f'/api/sessions/{sid}/generate',{})[0]==202
    assert started.wait(5)
    try:
        during=request(app,f'/api/sessions/{sid}')[1]
        assert during['active'] and during['stages']['generation']['status']=='running'
        assert request(app,f'/api/sessions/{sid}/generate',{})[0]==409
        assert request(app,f'/api/sessions/{sid}/edit',dict(proposal=hitl.FORMAT))[0]==409
        # A new browser connection consults the same durable session only.
        assert request(app,'/api/sessions')[1]['sessions'][0]['id']==sid
        assert len(calls)==1
    finally:finish.set()
    data=idle(app,sid)
    assert data['snapshot']['raw']==expected_raw
    assert data['snapshot']['hitl']['validation']['complete']
    assert not data['snapshot']['hitl']['approval_valid'] and data['snapshot']['result'] is None
    assert calls[0]['options']==hitl.agent.OPTIONS and calls[0]['messages']==hitl.request(web.PROSE)['messages']
    assert calls[0]['keep_alive']==0 and calls[0]['stream'] is False
    assert data['snapshot']['generation']['request']['keep_alive']==0
    assert 'thinking' not in data['snapshot']['generation']['response']['message']
    assert action(app,sid,'generate')['error']
    assert len(calls)==1


@pytest.mark.parametrize('raw',['not HITL','<script>alert(1)</script>',hitl.FORMAT.replace('cost=8 euro','cost=? euro')])
def test_invalid_llm_output_visible_never_approves(app,raw):
    app[0].client=lambda *a:dict(done=True,message=dict(content=raw))
    app[0].admission=lambda:dict(allowed=True)
    sid,_=create(app,web.PROSE)
    data=action(app,sid,'generate')
    assert data['snapshot']['raw']==raw
    assert not data['snapshot']['hitl']['validation']['complete']
    assert not data['snapshot']['hitl']['approval_valid']
    if 'cost=?' in raw:
        assert data['snapshot']['proposal_facts']['established']['resources']==['Machine_A','Machine_B']
        assert data['snapshot']['proposal_facts']['facts']


def test_no_ollama_preserves_manual_draft(app):
    app[0].admission=lambda:dict(allowed=False,reason='Ollama absent')
    sid,before=create(app,web.PROSE)
    data=action(app,sid,'generate')
    assert data['stages']['generation']['status']=='failed'
    assert data['snapshot']['proposal']==before['snapshot']['proposal']
    assert data['snapshot']['generation']['calls']==0
    proposal(app,sid);approve(app,sid)
    assert action(app,sid,'solve')['snapshot']['result']['solver']['objective']==12


@pytest.mark.parametrize('change,expected',[
    (lambda s:s.replace('capacity: 12.','capacity: 0.').replace('T10=0.','T10=1.'),'INFEASIBLE'),
    (lambda s:s,'UNKNOWN')])
def test_infeasible_or_zero_budget_no_assignment(app,change,expected):
    if expected=='UNKNOWN':app[0].seconds=0
    sid,data=create(app,change(web.EXAMPLE))
    assert not data['error'],data['error']
    result=data['snapshot']['result']
    assert result['solver']['raw_status']==expected
    assert result['solver']['candidate'] is None and not result['optimality_proven']
    assert not visualization.view(result)['assignments']
    assert request(app,f'/api/sessions/{sid}/graph.svg')[0]==200


def test_feasible_only_and_checker_failure(app,monkeypatch):
    original=hitl.solve_gap
    def feasible(*a,**kw):return dict(original(*a,**kw),raw_status='FEASIBLE',solver_proof=False)
    monkeypatch.setattr(hitl,'solve_gap',feasible)
    _,data=create(app)
    assert data['snapshot']['result']['outcome']=='feasible'
    assert not data['snapshot']['result']['optimality_proven']
    monkeypatch.setattr(hitl,'verify',lambda *a:dict(valid=False,violations=['injected']))
    sid,data=create(app)
    assert data['snapshot']['result']['outcome']=='verification_error'
    assert data['stages']['graphics']['status']=='failed'
    assert request(app,f'/api/sessions/{sid}/graph.svg')[0]==400


def test_svg_zero_capacity_unused_negative_long_name_and_html_escaping(app):
    source=web.EXAMPLE.replace('R10 capacity: 12.','R10 capacity: 0.').replace('T10','Task_with_a_very_long_identifier')
    _,data=create(app,source)
    result=data['snapshot']['result'];v=visualization.view(result)
    assert v['objective']==-5 and v['resources'][1]['load']==0 and v['resources'][1]['percent'] is None
    assert v['resources'][1]['tasks']==[]
    markup=visualization.svg(result)
    assert 'Task_with_a_very_long_identifier' in markup
    assert '<path' in markup and 'coût -' in markup
    assert 'aucune' in visualization.html(result)
    # Canonical unit labels allow general text; rendering must escape them.
    import copy
    bad=copy.deepcopy(result);bad['problem']['cost_unit']='<script>alert(1)</script>'
    assert '<script>' not in visualization.html(bad)
    assert '&lt;script&gt;' in visualization.html(bad)


def test_stale_revision_edit_token_approval_and_result_tampering(app):
    sid,_=create(app,web.PROSE)
    assert action(app,sid,'edit',dict(proposal=hitl.FORMAT,proposal_sha256='wrong'))['error']
    proposal(app,sid);approve(app,sid);action(app,sid,'solve')
    path=app[0].path(sid)/'session/result.json'
    result=load(path.read_bytes());result['solver']['objective']+=1
    hitl.write(path,hitl.encoded(result))
    assert request(app,f'/api/sessions/{sid}/result.json')[0]==400


@pytest.mark.parametrize('url',['/api/sessions/../original.txt','/api/sessions/'+'a'*31,'/api/import/../../'])
def test_paths_not_arbitrary_files(app,url):
    assert request(app,url)[0]==404
    assert request(app,'/api/import',dict(id='../../'))[0]==400
    assert request(app,'/api/import',dict(id=[]))[0]==400


def test_backend_scope_cannot_be_bypassed_via_legacy_hitl(app):
    sid,_=create(app,web.PROSE);proposal(app,sid)
    path=app[0].path(sid)/'session'
    review=hitl.review(path);token=review['review_token']
    with pytest.raises(ValueError,match='scope confirmation'):
        hitl.approve(path,token,'APPROVE:'+token,simulated=True)
    hitl.confirm_scope(path,token,'SCOPE:'+token,simulated=True)
    hitl.approve(path,token,'APPROVE:'+token,simulated=True)
    hitl.write(path/'proposal.txt',hitl.FORMAT.replace('cost=8 euro','cost=9 euro').encode())
    assert not hitl.status(path)['approval_valid']
    review=hitl.review(path);token=review['review_token']
    with pytest.raises(ValueError,match='scope confirmation'):
        hitl.approve(path,token,'APPROVE:'+token,simulated=True)


def test_restart_preserves_history_no_automatic_actions(tmp_path,monkeypatch):
    root=tmp_path/'restart'
    store=web.Store(root)
    sid=store.create(web.EXAMPLE)
    store.close()
    monkeypatch.setattr(hitl,'solve_gap',lambda *a,**kw:pytest.fail('restart solved'))
    monkeypatch.setattr(hitl,'propose',lambda *a,**kw:pytest.fail('restart generated'))
    store=web.Store(root)
    try:
        data=store.read(sid)
        assert data['snapshot']['result']['solver']['objective']==-5
        with store.condition:
            data['active']=True;store._event(data,'generation','running');store._save(data)
    finally:store.close()
    store=web.Store(root)
    try:
        data=store.read(sid)
        assert not data['active'] and 'interrompue' in data['error']
        assert data['stages']['generation']['status']=='failed'
    finally:store.close()


def test_shutdown_waits_for_generation_and_persists_result(tmp_path):
    start,finish=threading.Event(),threading.Event()
    def client(*a):start.set();assert finish.wait(10);return dict(done=True,message=dict(content=hitl.FORMAT))
    store=web.Store(tmp_path/'shutdown',client=client,admission=lambda:dict(allowed=True))
    sid=store.create(web.PROSE)
    while store.read(sid)['active']:time.sleep(.01)
    store.start(sid,'generate',{})
    assert start.wait(5)
    thread=threading.Thread(target=store.close);thread.start()
    assert store.stopping.wait(5) and thread.is_alive()
    with pytest.raises(web.Conflict):store.create(web.EXAMPLE)
    finish.set();thread.join(10);assert not thread.is_alive()
    store=web.Store(tmp_path/'shutdown')
    try:
        data=store.read(sid);assert not data['active'] and data['snapshot']['raw']==hitl.FORMAT
    finally:store.close()


def test_model_digest_residency_and_ram_guards(tmp_path,monkeypatch):
    store=web.Store(tmp_path/'guard')
    try:
        observed=dict(ollama=dict(available=True,models=[dict(name=web.agent.MODEL,digest='wrong')],loaded_models=[]))
        monkeypatch.setattr(web.resources,'snapshot',lambda *a,**kw:observed)
        assert not store._admit()['allowed']
        observed['ollama']['models'][0]['digest']=web.DIGEST
        observed['ollama']['loaded_models']=[dict(name='foreign')]
        assert not store._admit()['allowed']
        observed['ollama']['loaded_models']=[]
        from or_agent import extended_evaluate
        monkeypatch.setattr(extended_evaluate,'foreign_activity',lambda:dict(active=None))
        assert not store._admit()['allowed']
        monkeypatch.setattr(extended_evaluate,'foreign_activity',lambda:dict(active=False))
        monkeypatch.setattr(web.resources,'gate',lambda *a,**kw:dict(allowed=False,reason='RAM refused'))
        assert store._admit()['reason']=='RAM refused'
    finally:store.close()


def test_atomic_file_swap_survives_transient_windows_reader(tmp_path,monkeypatch):
    real=Path.replace
    calls=[]
    def locked_once(path,target):
        calls.append(1)
        if len(calls)==1:
            error=PermissionError('controlled Windows sharing violation')
            error.winerror=32
            raise error
        return real(path,target)
    monkeypatch.setattr(Path,'replace',locked_once)
    target=tmp_path/'state.json'
    hitl.write(target,b'{"valid":true}')
    assert target.read_bytes()==b'{"valid":true}' and len(calls)==2
    assert list(tmp_path.glob('*.tmp'))==[]


def test_delete_local_artifacts_preserves_other_sessions_and_import_source(app, monkeypatch):
    store, _ = app
    OLD = store.imports['0']
    original = {p.name: p.read_bytes() for p in OLD.iterdir() if p.is_file()}
    other, _ = create(app)
    other_path = store.path(other)
    preserved = {p.relative_to(other_path): p.read_bytes() for p in other_path.rglob('*') if p.is_file()}
    code, out = request(app, '/api/import', dict(id='0'))
    assert code == 202
    sid = out['id']
    idle(app, sid)
    body = ready_to_resolve(app, sid)
    data = action(app, sid, 'resolve', body)
    assert data['snapshot']['result']['solver']['objective'] == 12
    path = store.path(sid)
    assert (path/'session/approved').is_dir() and (path/'session/result.json').is_file()
    exports = path/'exports/nested'
    exports.mkdir(parents=True)
    (exports/'graph.svg').write_text('<svg/>')
    with monkeypatch.context() as dispatch:
        dispatch.setattr(store, 'start', lambda *a, **k: pytest.fail('deletion reached the job dispatcher'))
        assert request(app, f'/api/sessions/{sid}/delete', {})[0] == 400
        assert request(app, f'/api/sessions/{sid}/delete', dict(confirmation='DELETE:'+other))[0] == 400
        assert path.exists()
        assert request(app, f'/api/sessions/{sid}/delete', dict(confirmation='DELETE:'+sid)) == (200, dict(id=sid, deleted=True))
    assert not path.exists()
    assert [s['id'] for s in request(app, '/api/sessions')[1]['sessions']] == [other]
    for suffix in ('', '/events', '/result.json', '/graph.svg', '/result.html'):
        assert request(app, f'/api/sessions/{sid}{suffix}')[0] == 404
    assert request(app, f'/api/sessions/{sid}/solve', {})[0] == 404
    assert {p.relative_to(other_path): p.read_bytes() for p in other_path.rglob('*') if p.is_file()} == preserved
    assert {p.name: p.read_bytes() for p in OLD.iterdir() if p.is_file()} == original


def test_delete_missing_session_preserves_existing_session(app):
    store, _ = app
    survivor, _ = create(app)
    path = store.path(survivor)
    before = {p.relative_to(path): p.read_bytes() for p in path.rglob('*') if p.is_file()}
    missing = '0'*32
    for _ in range(2):
        code, data = request(app, f'/api/sessions/{missing}/delete', dict(confirmation='DELETE:'+missing))
        assert code == 404 and 'FileNotFoundError' in data['error']
    assert [s['id'] for s in store.listing()['sessions']] == [survivor]
    assert {p.relative_to(path): p.read_bytes() for p in path.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('sid', ['..', '../outside', 'a'*31, 'a'*32+'/..', '/tmp', 'a'*32+'%2f..'])
def test_delete_rejects_path_traversal(app, sid):
    with pytest.raises((ValueError, FileNotFoundError)):
        app[0].delete(sid, 'DELETE:'+sid)
    assert request(app, f'/api/sessions/{sid}/delete', dict(confirmation='DELETE:'+sid))[0] == 404


def directory_link(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != 'nt':
            raise
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], check=True, capture_output=True)


@pytest.mark.parametrize('location', ['session_root', 'nested', 'other_session'])
def test_delete_refuses_links_before_removing_anything(app, tmp_path, location):
    store, _ = app
    sid, _ = create(app)
    path = store.path(sid)
    outside = tmp_path/'protected-history'
    outside.mkdir()
    (outside/'proof.txt').write_text('preserve historical evidence')
    if location == 'session_root':
        link = store.root/('f'*32)
        directory_link(link, outside)
        target_id = link.name
    elif location == 'other_session':
        other, _ = create(app)
        link = path/'linked-session'
        directory_link(link, store.path(other))
        target_id = sid
    else:
        link = path/'session/linked-history'
        directory_link(link, outside)
        target_id = sid
    before = {p.relative_to(path): p.read_bytes() for p in (path/'session').glob('*') if p.is_file()}
    try:
        assert request(app, f'/api/sessions/{target_id}/delete', dict(confirmation='DELETE:'+target_id))[0] == 400
        assert (outside/'proof.txt').read_text() == 'preserve historical evidence'
        assert {p.relative_to(path): p.read_bytes() for p in (path/'session').glob('*') if p.is_file()} == before
        assert (path/'web.json').exists()
        if location == 'other_session':
            assert store.read(other)['snapshot']['result']['solver']['objective'] == -5
    finally:
        if link.is_symlink():
            link.unlink()
        else:
            link.rmdir()  # Windows junction itself, never its target.


def test_delete_refuses_shared_hardlink(app, tmp_path):
    sid, _ = create(app)
    outside = tmp_path/'shared-proof.txt'
    outside.write_text('shared')
    link = app[0].path(sid)/'shared.txt'
    os.link(outside, link)
    assert request(app, f'/api/sessions/{sid}/delete', dict(confirmation='DELETE:'+sid))[0] == 400
    assert outside.read_text() == link.read_text() == 'shared'


@pytest.mark.parametrize('operation', ['artifacts', 'directory'])
def test_delete_reports_disk_failure_keeps_registry(app, monkeypatch, operation):
    sid, _ = create(app)
    path = app[0].path(sid)
    if operation == 'artifacts':
        def fail(*args, **kwargs):
            raise PermissionError('controlled disk failure')
        monkeypatch.setattr(web.shutil, 'rmtree', fail)
    else:
        real = Path.rmdir
        def fail(entry):
            if entry == path:
                raise PermissionError('controlled disk failure')
            return real(entry)
        monkeypatch.setattr(Path, 'rmdir', fail)
    code, data = request(app, f'/api/sessions/{sid}/delete', dict(confirmation='DELETE:'+sid))
    assert code == 500 and 'Suppression incomplète sur disque' in data['error']
    assert 'deleted' not in data and (path/'web.json').is_file()
    assert sid in [s['id'] for s in app[0].listing()['sessions']]
    monkeypatch.undo()
    assert request(app, f'/api/sessions/{sid}/delete', dict(confirmation='DELETE:'+sid))[0] == 200
    assert not path.exists()


@pytest.mark.parametrize('job', ['generate', 'resolve'])
def test_delete_and_duplicate_actions_refused_while_active(app, monkeypatch, job):
    sid, _ = create(app, web.PROSE)
    started, finish = threading.Event(), threading.Event()
    calls = []
    def block(*args, **kwargs):
        calls.append(1)
        started.set()
        assert finish.wait(10)
        if job == 'generate':
            return dict(done=True, message=dict(content=hitl.FORMAT))
        return original(*args, **kwargs)
    body = {}
    if job == 'generate':
        app[0].client = block
        app[0].admission = lambda: dict(allowed=True)
    else:
        proposal(app, sid)
        body = ready_to_resolve(app, sid)
        original = hitl.solve_gap
        monkeypatch.setattr(hitl, 'solve_gap', block)
    assert request(app, f'/api/sessions/{sid}/{job}', body)[0] == 202
    assert started.wait(5)
    try:
        assert request(app, f'/api/sessions/{sid}/delete', dict(confirmation='DELETE:'+sid))[0] == 409
        assert request(app, f'/api/sessions/{sid}/{job}', body)[0] == 409
        assert app[0].path(sid).exists()
    finally:
        finish.set()
    data = idle(app, sid)
    assert not data['error'], data['error']
    if job == 'resolve':
        assert not action(app, sid, job, body)['error']
        assert data['snapshot']['result']['fingerprints'] == data['snapshot']['hitl']['fingerprints']
    assert len(calls) == 1


def test_resolve_confirms_approves_then_solves_same_sha(app):
    code, out = request(app, '/api/import', dict(id='0'))
    assert code == 202
    sid = out['id']
    idle(app, sid)
    body = ready_to_resolve(app, sid)
    data = action(app, sid, 'resolve', body)
    assert not data['error'], data['error']
    state = data['snapshot']
    assert state['hitl']['approval_valid'] and state['result_current']
    assert state['result']['fingerprints'] == state['hitl']['fingerprints']
    assert state['result']['fingerprints']['canonical_sha256'] == state['hitl']['approval']['canonical_sha256']
    events = hitl.load(app[0].path(sid)/'session')[0].events
    assert [e['action'] for e in events if e['action'] in ('approved', 'solved')] == ['approved', 'solved']
    assert state['result']['solver']['objective'] == 12


@pytest.mark.parametrize('failure', ['missing_confirmation', 'stale_token', 'missing_scope', 'edit', 'rejected', 'invalid_return'])
def test_failed_approval_never_calls_solver(app, monkeypatch, failure):
    sid, _ = create(app, web.PROSE)
    proposal(app, sid)
    if failure == 'missing_scope':
        data = action(app, sid, 'review')
        token = data['snapshot']['hitl']['review_token']
        body = dict(review_token=token, confirmation='APPROVE:'+token)
    else:
        body = ready_to_resolve(app, sid)
    if failure == 'missing_confirmation':
        body.pop('confirmation')
    elif failure == 'stale_token':
        body = dict(review_token='bad', confirmation='APPROVE:bad')
    elif failure == 'edit':
        # Old confirmation cannot approve modified costs/canonical data.
        first = action(app, sid, 'resolve', body)
        old_sha = first['snapshot']['hitl']['fingerprints']['canonical_sha256']
        changed = proposal(app, sid, hitl.FORMAT.replace('cost=8 euro', 'cost=9 euro'))
        assert not changed['snapshot']['hitl']['approval_valid']
        assert changed['snapshot']['hitl']['fingerprints']['canonical_sha256'] != old_sha
    elif failure == 'rejected':
        action(app, sid, 'reject', dict(reason='simulated rejection'))
    elif failure == 'invalid_return':
        monkeypatch.setattr(hitl, 'approve', lambda *a, **k: dict(approval_valid=False, review_token=body['review_token']))
    monkeypatch.setattr(routing, 'solve', lambda *a, **k: pytest.fail('solver called after failed approval'))
    assert action(app, sid, 'resolve', body)['error']


def test_deleted_session_absent_after_restart(tmp_path, monkeypatch):
    root = tmp_path/'restart-delete'
    store = web.Store(root)
    removed = store.create(web.EXAMPLE)
    survivor = store.create(web.EXAMPLE)
    store.close()
    store = web.Store(root)
    store.delete(removed, 'DELETE:'+removed)
    store.close()
    monkeypatch.setattr(hitl, 'solve_gap', lambda *a, **k: pytest.fail('restart solved'))
    monkeypatch.setattr(hitl, 'propose', lambda *a, **k: pytest.fail('restart generated'))
    store = web.Store(root)
    try:
        assert [s['id'] for s in store.listing()['sessions']] == [survivor]
        assert not (root/removed).exists()
        with pytest.raises(FileNotFoundError):
            store.read(removed)
        assert store.read(survivor)['snapshot']['result']['solver']['objective'] == -5
    finally:
        store.close()


def test_browser_session_deletion_and_combined_approval(app, tmp_path):
    script = Path(__file__).with_name('web_browser.mjs')
    assert script.is_file(), 'Missing browser scenario: tests/web_browser.mjs'
    # Browser startup and rendering share the host; retain a bounded solver budget.
    app[0].seconds = 15
    original = app[0].imports['0']
    preserved = {p.relative_to(original): p.read_bytes() for p in original.rglob('*') if p.is_file()}
    calls, admissions = [], []
    def client(payload, timeout):
        calls.append(payload)
        return dict(done=True, message=dict(content=hitl.FORMAT))
    def admission():
        admissions.append(True)
        return dict(allowed=len(admissions) > 1, reason='Controlled Ollama unavailable')
    app[0].client, app[0].admission = client, admission
    readme = Path('README.md').read_text(encoding='utf-8')
    example = readme.split('### Exemple sans LLM', 1)[1].split('```text\n', 1)[1].split('```', 1)[0].strip()
    fixtures = tmp_path/'browser-fixtures.json'
    fixtures.write_text(json.dumps(dict(source=example, prose=web.PROSE, proposal=hitl.FORMAT,
        problem=load(Path('examples/small.json').read_text(encoding='utf-8')))), encoding='utf-8')
    node = shutil.which('node')
    chrome = os.environ.get('OR_AGENT_CHROME') or shutil.which('google-chrome') or shutil.which('chromium') or shutil.which('chromium-browser')
    if not chrome and sys.platform == 'win32':
        chrome = next((str(p) for p in (
            Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),
            Path('C:/Program Files (x86)/Google/Chrome/Application/chrome.exe')) if p.exists()), None)
    assert node and chrome, 'Install Node >=22 and Chrome/Chromium, or set OR_AGENT_CHROME'
    profile = tmp_path/'browser-profile'
    browser = subprocess.Popen([chrome, '--headless=new', '--disable-gpu', '--no-first-run',
        '--no-default-browser-check', '--remote-debugging-port=0', '--remote-allow-origins=*',
        '--user-data-dir='+str(profile), 'about:blank'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        port_file = profile/'DevToolsActivePort'
        deadline = time.monotonic()+15
        while not port_file.exists() and time.monotonic()<deadline:
            time.sleep(.05)
        assert port_file.exists(), 'Chrome debugger did not start'
        port = port_file.read_text().splitlines()[0]
        result = subprocess.run([node, str(script),
            f'http://127.0.0.1:{app[1].server_port}', f'http://127.0.0.1:{port}', str(fixtures)],
            capture_output=True, text=True, encoding='utf-8', timeout=120)
        assert result.returncode == 0, result.stdout+'\n'+result.stderr
        print(result.stdout)
        assert len(calls) == 1 and len(admissions) == 2
        assert app[0].listing()['sessions'] == []
        assert not any(p.is_dir() for p in app[0].root.iterdir())
        assert {p.relative_to(original): p.read_bytes() for p in original.rglob('*') if p.is_file()} == preserved
    finally:
        browser.terminate()
        browser.wait(timeout=10)
