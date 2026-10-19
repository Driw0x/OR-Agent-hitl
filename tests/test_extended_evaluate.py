"""Offline runner checks: frozen identities, resumption, selection and resource refusal."""

from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from urllib.error import URLError

import pytest

from or_agent import extended_evaluate as experiment
from or_agent.agent import answer_for
from or_agent.models import Gap
from or_agent.verify import verify


def case(identifier, split='dev', cost=2):
    problem = dict(tasks=['T'], resources=['R'], costs=[[cost]], consumption=[[1]], capacities=[1],
                   cost_unit='EUR', resource_unit='slot')
    source = json.dumps(problem)
    return dict(id=identifier, split=split, category='simple', family=identifier, source=source,
                disposition='ready', problem=problem, reference_optimum=cost, reference_feasible_count=1)


def result(reference, method='structured', model='small', elapsed=0.25, wrong=False, failure=False):
    problem = deepcopy(reference['problem'])
    if wrong:
        problem['costs'][0][0] += 1
    extraction = dict(disposition='ready', problem=problem, issues=[],
                      provenance={key: reference['source'] for key in problem})
    candidate = dict(assignments=[dict(task='T', resource='R')], objective=problem['costs'][0][0])
    output = dict(outcome='verified', reason='independent_check_passed', candidate=candidate,
                  optimality_proven=True, source=reference['source'], extraction=extraction,
                  verification=verify(Gap.model_validate(problem), candidate),
                  solver=dict(raw_status='OPTIMAL', solver_proof=True, objective=candidate['objective'],
                              best_bound=candidate['objective'], solver_wall_seconds=0.001,
                              parameters=dict(absolute_gap_limit=0, relative_gap_limit=0)),
                  attempts=[dict(raw=json.dumps(extraction), json_valid=True, structural_valid=True,
                                 response=dict(eval_duration=int(elapsed * 1e9), load_duration=0,
                                               prompt_eval_count=20, eval_count=10))],
                  observations=[], elapsed_seconds=elapsed, method=method, model=model)
    if failure:
        output.update(outcome='execution_error', reason='TimeoutError: synthetic', candidate=None,
                      extraction=None, optimality_proven=False, attempts=[])
    output['answer'] = answer_for(output)
    return output


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    cases = [case('dev1'), case('dev2', cost=3), case('test1', 'test', 4)]
    data_path = tmp_path / 'corpus.json'
    experiment.save(data_path, dict(cases=cases))
    models = [dict(name='small', digest='small-digest', size=10, capabilities=['completion']),
              dict(name='large', digest='large-digest', size=20, capabilities=['completion'])]
    hardware = dict(ollama=dict(available=True, models=models, loaded_models=[]))
    monkeypatch.setattr(experiment, 'ROOT', tmp_path)
    (tmp_path / 'data').mkdir()
    (tmp_path / 'or_agent').mkdir()
    (tmp_path / 'or_agent' / 'frozen.py').write_text('# synthetic source\n', encoding='utf-8')
    experiment.save(tmp_path / 'data' / 'benchmark.json', dict(cases=[]))
    monkeypatch.setattr(experiment, 'audit', lambda data, **kwargs: dict(valid=True, dev_count=2, test_count=1))
    monkeypatch.setattr(experiment, 'snapshot', lambda *args, **kwargs: deepcopy(hardware))
    monkeypatch.setattr(experiment, 'urlopen', lambda *args, **kwargs: BytesIO(b'{"version":"offline"}'))
    manifest_path = tmp_path / 'selection.json'
    manifest = experiment.freeze_selection(data_path, manifest_path)
    calls = []
    def fake_run(source, method, model, base_url, **kwargs):
        calls.append((source, method, model, base_url, kwargs))
        reference = next(c for c in cases if c['source'] == source)
        return result(reference, method, model)
    monkeypatch.setattr(experiment, 'run', fake_run)
    return dict(cases=cases, data=data_path, manifest=manifest, path=manifest_path,
                output=tmp_path / 'selection.jsonl', calls=calls, hardware=hardware)


def test_freeze_before_measurement_and_refuse_changed_dataset(fixture):
    f = fixture
    assert f['manifest']['options'] == experiment.OPTIONS
    assert f['manifest']['ranking'] == experiment.RANKING
    assert all(item['method'] == 'structured' and item['case_id'].startswith('dev') for item in f['manifest']['planned'])
    assert len(f['manifest']['planned']) == 4
    assert experiment.verify_manifest(f['path'])['valid']
    with pytest.raises(FileExistsError):
        experiment.freeze_selection(f['data'], f['path'])
    f['data'].write_bytes(f['data'].read_bytes() + b' ')
    with pytest.raises(ValueError, match='dataset changed'):
        experiment.selection(f['path'], f['output'])
    assert f['calls'] == []


def test_pilot_observations_reused_and_resume_never_replays_completed(fixture):
    f = fixture
    first = experiment.selection(f['path'], f['output'], pilot=True)
    assert (first['completed'], first['planned']) == (2, 4)
    assert [call[2] for call in f['calls']] == ['small', 'large']
    original = f['output'].read_bytes()
    final = experiment.selection(f['path'], f['output'], resume=True)
    assert final['completed'] == 4 and len(f['calls']) == 4
    assert f['output'].read_bytes().startswith(original)
    _, _, completed, _ = experiment.records(f['output'])
    assert set(completed) == {0, 1, 2, 3}
    assert all(call[1] == 'structured' and call[3].endswith(':11435') for call in f['calls'])
    frozen_bytes = f['output'].read_bytes()
    experiment.selection(f['path'], f['output'], resume=True)
    assert f['output'].read_bytes() == frozen_bytes
    assert len(f['calls']) == 4
    metrics = experiment.replay(f['output'])
    assert set(metrics['candidates']) == {'small', 'large'}
    assert all(m['methods']['structured']['final_answer_correct']['count'] == 2 for m in metrics['candidates'].values())


def test_changed_source_refuses_resume_before_any_model_call(fixture):
    f = fixture
    source = f['path'].parent / 'or_agent' / 'frozen.py'
    source.write_text('# changed after freeze\n', encoding='utf-8')
    with pytest.raises(ValueError, match='source changed'):
        experiment.selection(f['path'], f['output'])
    assert f['calls'] == []


def test_selection_prioritizes_correctness_over_speed_and_freezes_test(fixture, monkeypatch):
    f = fixture
    def varying_run(source, method, model, base_url, **kwargs):
        reference = next(c for c in f['cases'] if c['source'] == source)
        return result(reference, method, model, elapsed=0.001 if model == 'small' else 1,
                      wrong=model == 'small')
    monkeypatch.setattr(experiment, 'run', varying_run)
    experiment.selection(f['path'], f['output'])
    choice = experiment.select(f['path'], f['output'])
    assert choice['selected']['model'] == 'large'
    test_path = f['path'].parent / 'test.json'
    frozen = experiment.freeze_test(f['path'], f['output'], test_path)
    assert frozen['selection_records_sha256'] == experiment.digest(f['output'])
    assert len(frozen['planned']) == 3 and len(frozen['repetition_plan']) == 6
    assert {p['case_id'] for p in frozen['planned']} == {'test1'}
    assert {p['method'] for p in frozen['planned']} == {'json', 'structured', 'structured-repair'}
    assert experiment.verify_manifest(test_path)['valid']
    main = f['path'].parent / 'main.jsonl'
    experiment.campaign(test_path, main, limit=1)
    experiment.campaign(test_path, main, resume=True)
    assert experiment.replay(main)['completed_observations'] == 3
    with pytest.raises(ValueError, match='300 primary'):
        experiment.campaign(test_path, f['path'].parent / 'repeats.jsonl', repetitions=3, primary_records=main)


def test_selection_does_not_claim_single_candidate_comparison(fixture, monkeypatch):
    f = fixture
    def blocked_run(source, method, model, base_url, **kwargs):
        reference = next(c for c in f['cases'] if c['source'] == source)
        output = result(reference, method, model, failure=model == 'large')
        if model == 'large':
            output.update(outcome='resource_blocked', reason='structural_ram_limit')
            output['answer'] = answer_for(output)
        return output
    monkeypatch.setattr(experiment, 'run', blocked_run)
    experiment.selection(f['path'], f['output'])
    choice = experiment.select(f['path'], f['output'])
    assert choice['comparable_candidates'] == 1
    assert 'no comparative superiority' in choice['comparison']
    assert choice['ranking'][1]['status'] == 'resource_blocked'
    assert choice['ranking'][1]['completed'] == 1


def test_selection_refuses_unobserved_or_partially_evaluated_candidates(fixture):
    f = fixture
    experiment.selection(f['path'], f['output'], limit=2)
    with pytest.raises(ValueError, match='selection is unfinished'):
        experiment.select(f['path'], f['output'])


def test_model_switch_unloads_only_the_model_used_by_this_campaign(fixture, monkeypatch):
    f = fixture
    payloads = []
    def fake_run(source, method, model, base_url, **kwargs):
        f['hardware']['ollama']['loaded_models'] = [dict(name=model)]
        return result(next(c for c in f['cases'] if c['source'] == source), method, model)
    def unload(request, **kwargs):
        payloads.append(json.loads(request.data))
        f['hardware']['ollama']['loaded_models'] = []
        return BytesIO(b'{"done":true}')
    monkeypatch.setattr(experiment, 'run', fake_run)
    monkeypatch.setattr(experiment, 'urlopen', unload)
    experiment.selection(f['path'], f['output'])
    assert payloads == [dict(model='small', keep_alive=0)]
    _, rows, _, _ = experiment.records(f['output'])
    transition = next(row for row in rows if row['kind'] == 'unload_owned_model')
    assert transition['before']['ollama']['loaded_models'] == [dict(name='small')]
    assert transition['after']['ollama']['loaded_models'] == []


def test_timeout_circuit_breaker_stops_and_requires_idle_before_resume(fixture, monkeypatch):
    f = fixture
    monkeypatch.setattr(experiment, 'run', lambda source, method, model, base_url, **kwargs:
                        result(f['cases'][0], method, model, failure=True))
    partial = experiment.selection(f['path'], f['output'])
    assert partial['completed'] == 1 and partial['reason'] == 'execution_error_circuit_breaker'
    with pytest.raises(ValueError, match='unknown server activity'):
        experiment.selection(f['path'], f['output'], resume=True)
    monkeypatch.setattr(experiment, 'run', lambda source, method, model, base_url, **kwargs:
                        result(next(c for c in f['cases'] if c['source'] == source), method, model))
    resumed = experiment.selection(f['path'], f['output'], resume=True, idle_verified=True)
    assert resumed['completed'] == 4
    assert experiment.select(f['path'], f['output'])['selected']['model'] == 'large'


def test_offline_replay_rejects_tampered_metrics_and_result_identity(fixture):
    f = fixture
    experiment.selection(f['path'], f['output'])
    lines = [json.loads(line) for line in f['output'].read_text().splitlines()]
    row = next(row for row in lines if row['kind'] == 'result')
    row['metrics']['final_answer_correct'] = False
    f['output'].write_text('\n'.join(json.dumps(row) for row in lines) + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='metrics differ'):
        experiment.replay(f['output'])
    row['metrics']['final_answer_correct'] = True
    row['model_digest'] = 'other-digest'
    f['output'].write_text('\n'.join(json.dumps(row) for row in lines) + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='identity differs'):
        experiment.replay(f['output'])


def test_selection_cannot_replace_development_plan_with_held_out_cases(fixture):
    f = fixture
    experiment.selection(f['path'], f['output'])
    lines = [json.loads(line) for line in f['output'].read_text().splitlines()]
    lines[0]['planned'][0]['case_id'] = 'test1'
    f['output'].write_text('\n'.join(json.dumps(row) for row in lines) + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='plan differs'):
        experiment.select(f['path'], f['output'])


def test_orphan_start_requires_idle_and_completed_rows_stay_immutable(fixture):
    f = fixture
    experiment.selection(f['path'], f['output'], limit=0)
    with f['output'].open('a', encoding='utf-8') as file:
        file.write(json.dumps(dict(kind='start', index=0, **f['manifest']['planned'][0])) + '\n')
    with pytest.raises(ValueError, match='unknown server activity'):
        experiment.selection(f['path'], f['output'], resume=True)
    experiment.selection(f['path'], f['output'], resume=True, idle_verified=True)
    assert len(f['calls']) == 4


def test_foreign_model_waits_then_releases_native_gate(monkeypatch):
    activity = iter([dict(active=True, models=['foreign']), dict(active=False, models=[])])
    monkeypatch.setattr(experiment, 'foreign_activity', lambda: next(activity))
    monkeypatch.setattr(experiment.time, 'sleep', lambda seconds: None)
    calls = []
    monkeypatch.setattr(experiment, 'gate', lambda *args, **kwargs: calls.append(args) or dict(allowed=True))
    budget = experiment.WaitBudget()
    outcome = experiment.admission('small', 'http://localhost:11435', budget, experiment.time.monotonic() + 600)
    assert outcome['allowed'] and outcome['foreign_wait_seconds'] >= 60
    assert budget.used_seconds >= 60 and len(calls) == 1


def test_foreign_activity_unknown_never_loads_model(monkeypatch):
    monkeypatch.setattr(experiment, 'foreign_activity', lambda: dict(active=None, models=None))
    monkeypatch.setattr(experiment.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(experiment, 'gate', lambda *args, **kwargs: pytest.fail('must not admit a model'))
    budget = experiment.WaitBudget(total_seconds=60, operation_seconds=60)
    outcome = experiment.admission('small', 'http://localhost:11435', budget, experiment.time.monotonic() + 600)
    assert not outcome['allowed'] and outcome['reason'] == 'foreign_ollama_activity_unknown'
    assert budget.used_seconds == 60


def test_closed_shared_port_is_distinguished_from_timeout(monkeypatch):
    monkeypatch.setattr(experiment, 'host_ollama', lambda: dict(valid=True, processes=[],
                        listeners=[dict(LocalPort=11434, OwningProcess=2)]))
    def refused(*args, **kwargs):
        raise URLError(ConnectionRefusedError('closed port'))
    monkeypatch.setattr(experiment, 'urlopen', refused)
    assert experiment.foreign_activity()['active'] is False
    def timed_out(*args, **kwargs):
        raise URLError(TimeoutError('no activity information'))
    monkeypatch.setattr(experiment, 'urlopen', timed_out)
    assert experiment.foreign_activity()['active'] is None


def test_foreign_server_on_another_port_blocks_even_when_shared_port_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(experiment, 'ROOT', tmp_path)
    monkeypatch.setattr(experiment, 'host_ollama', lambda: dict(valid=True,
                        processes=[dict(ProcessId=22, ParentProcessId=1, CommandLine='ollama.exe serve')],
                        listeners=[dict(LocalPort=21555, OwningProcess=22)]))
    endpoints = []
    def ps(endpoint, **kwargs):
        endpoints.append(endpoint)
        return BytesIO(b'{"models":[{"name":"foreign-model"}]}')
    monkeypatch.setattr(experiment, 'urlopen', ps)
    state = experiment.foreign_activity()
    assert state['active'] is True
    assert endpoints == ['http://127.0.0.1:21555/api/ps']


def test_repetition_rule_never_consults_scores_and_refuses_failure_or_time(monkeypatch):
    plan = [dict(case_id=f'case{index}', method='json', repetition=1) for index in range(300)]
    header = dict(phase='test', manifest_sha256='frozen', planned=plan)
    rows = {i: dict(result=dict(outcome='abstain', reason='mathematical failure', elapsed_seconds=1),
                    metrics=dict(final_answer_correct=False)) for i in range(300)}
    monkeypatch.setattr(experiment, 'records', lambda path: (header, [], rows, []))
    monkeypatch.setattr(experiment, 'digest', lambda path: 'frozen')
    decision = experiment.repeat_eligibility('main.jsonl', 'protocol.json')
    assert decision['eligible'] and decision['projected_900_run_seconds'] == 900
    rows[0]['result']['outcome'] = 'resource_blocked'
    with pytest.raises(ValueError, match='condition fails'):
        experiment.repeat_eligibility('main.jsonl', 'protocol.json')
    rows[0]['result'].update(outcome='abstain', elapsed_seconds=4000)
    with pytest.raises(ValueError, match='condition fails'):
        experiment.repeat_eligibility('main.jsonl', 'protocol.json')


def test_history_protects_artifacts_but_reports_authorized_docs(tmp_path, monkeypatch):
    monkeypatch.setattr(experiment, 'ROOT', tmp_path)
    artifact, document = tmp_path / 'historical.json', tmp_path / 'README.md'
    artifact.write_text('original'); document.write_text('original')
    baseline = tmp_path / 'baseline.json'
    experiment.save(baseline, dict(files={p.name: experiment.digest(p) for p in (artifact, document)}))
    document.write_text('updated')
    assert experiment.history(baseline)['valid']
    artifact.write_text('changed')
    assert experiment.history(baseline)['protected_changes'] == ['historical.json']
