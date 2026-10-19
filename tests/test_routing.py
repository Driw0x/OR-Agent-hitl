"""Offline routing, mathematical checks and explicitly simulated approvals."""

from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from or_agent import deterministic as d, hitl, routing as r, web
from or_agent.models import Gap

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail('offline routing attempted network')
    monkeypatch.setattr(socket.socket, 'connect', deny)


def source():
    return web.EXAMPLE


def partial():
    return '\n'.join(line for line in source().splitlines() if not line.startswith('R1 capacity:'))


def approved(path):
    r.submit(path, partial().encode())
    proposal = r.manual_proposal(d.extract(source()))
    (path / 'proposal.txt').write_text(proposal, encoding='utf-8')
    review = hitl.review(path)
    token = review['review_token']
    hitl.approve(path, token, 'APPROVE:' + token, simulated=True)
    return review['validation']['problem']


@pytest.mark.parametrize('complete', [True, False])
def test_admission_matches_completeness(complete):
    original = source() if complete else partial()
    report = d.extract(original)
    cert = r.certify(original, report)
    assert (cert['status'] == 'VERIFIED_COMPLETE') == complete
    if complete:
        assert all(cert['checks'].values()) and not cert['blockers']
        assert report['complete_problem']['costs'] == [[-1, -4], [8, 22]]
    else:
        assert cert['blockers']


def test_tampered_report_never_certified():
    report = d.extract(source())
    report['complete_problem']['costs'][0][0] += 1
    with pytest.raises(ValueError, match='does not match'):
        r.certify(source(), report)


@pytest.mark.parametrize('llm', [False, True])
def test_autonomous_no_model_no_data_change(tmp_path, monkeypatch, llm):
    def forbidden(*args, **kwargs):
        pytest.fail('autonomous branch touched Ollama')
    monkeypatch.setattr(hitl, 'propose', forbidden)
    expected = d.extract(source())['complete_problem']
    result = r.submit(tmp_path / 'auto', source().encode(), llm=llm, client=forbidden, admission=forbidden)
    assert result['routing']['branch'] == 'AUTONOMOUS'
    assert result['result']['problem'] == expected
    assert result['result']['outcome'] == 'optimal'
    assert result['result']['verification']['valid']
    assert result['result']['optimality_proven']
    assert result['result']['llm_calls'] == 0
    assert not (tmp_path / 'auto' / 'generation.json').exists()


def test_unknown_scope_cannot_be_approved_away(tmp_path):
    original = source() + '\nLa tâche T1 doit obligatoirement être exécutée avant T10.'
    path = tmp_path / 'scope'
    output = r.submit(path, original.encode())
    assert output['routing']['branch'] == 'HITL' and not output['result']
    assert output['hitl']['validation']['source_blockers']
    # Removing every Issue and replacing with a perfect GAP does not remove
    # original-source constraints, including via the old hitl command.
    (path / 'proposal.txt').write_text(r.manual_proposal(d.extract(source())), encoding='utf-8')
    review = hitl.review(path)
    token = review['review_token']
    with pytest.raises(ValueError, match='incomplete or invalid'):
        hitl.approve(path, token, 'APPROVE:' + token, simulated=True)
    with pytest.raises(ValueError, match='approval'):
        r.solve(path)
    with pytest.raises(ValueError, match='approval'):
        hitl.solve(path)


@pytest.mark.parametrize('extra', ['R1 capacity: 1.', 'Resource unit: minute.', 'Preamble: use R1 first.'])
def test_original_contradictions_or_scope_require_source_correction(tmp_path, extra):
    path = tmp_path / 'invalid'
    r.submit(path, (source() + '\n' + extra).encode())
    (path / 'proposal.txt').write_text(r.manual_proposal(d.extract(source())), encoding='utf-8')
    assert not hitl.validate(path)['validation']['complete']
    # Explicitly submitting corrected source creates new independent authority.
    fixed = r.submit(tmp_path / 'corrected', source().encode())
    assert fixed['result']['outcome'] == 'optimal'


def test_missing_data_manual_correction_simulated_confirmation(tmp_path, monkeypatch):
    path = tmp_path / 'human'
    result = r.submit(path, partial().encode())
    assert result['routing']['branch'] == 'HITL' and not result['result']
    assert '? hour.' in (path / 'proposal.txt').read_text()
    with pytest.raises(ValueError, match='approval'):
        r.solve(path)
    (path / 'proposal.txt').write_text(r.manual_proposal(d.extract(source())), encoding='utf-8')
    review = hitl.review(path)
    token = review['review_token']
    with pytest.raises(ValueError, match='explicit confirmation'):
        hitl.approve(path, token, 'yes', simulated=True)
    hitl.approve(path, token, 'APPROVE:' + token, simulated=True)
    monkeypatch.setattr(hitl.agent, 'ollama_chat', lambda *a: pytest.fail('LLM after approval'))
    solved = r.solve(path)
    assert solved['approved_problem'] == review['validation']['problem']
    assert solved['verification']['valid'] and solved['approval_actor'] == 'simulated_reviewer'
    assert r.status(path)['result_current']


@pytest.mark.parametrize('before,after', [
    ('Cost unit: euro.', 'Cost unit: dollar.'),
    ('R1: 12 hour.', 'R1: 11 hour.'),
    (hitl.CONSTRAINTS, hitl.CONSTRAINTS + '\nIssue: New precedence constraint.'),
])
def test_changes_invalidate_approval(tmp_path, before, after):
    path = tmp_path / 'changed'
    approved(path)
    proposal = (path / 'proposal.txt').read_text(encoding='utf-8')
    assert before in proposal
    (path / 'proposal.txt').write_text(proposal.replace(before, after), encoding='utf-8')
    assert not hitl.status(path)['approval_valid']
    with pytest.raises(ValueError, match='approval'):
        r.solve(path)


def test_change_after_solve_retains_history_but_not_current(tmp_path):
    path = tmp_path / 'changed'
    approved(path)
    r.solve(path)
    with (path / 'proposal.txt').open('a', encoding='utf-8') as output:
        output.write('\nIssue: correction pending\n')
    assert not r.status(path)['result_current']
    assert not r.status(path)['hitl']['approval_valid']


@pytest.mark.parametrize('filename', ['original.txt', 'routing.json', 'canonical.json', 's2.json', 'result.json'])
def test_autonomous_evidence_tamper(tmp_path, filename):
    path = tmp_path / 'tampered'
    r.submit(path, source().encode())
    target = path / filename
    if filename == 'original.txt':
        target.write_bytes(target.read_bytes() + b'\n')
    else:
        content = json.loads(target.read_text(encoding='utf-8'))
        if filename == 'routing.json': content['certificate']['checks']['units_checked'] = False
        elif filename == 'canonical.json': content['costs'][0][0] += 1
        elif filename == 's2.json': content['facts'][0]['value'] += 1
        else: content['outcome'] = 'infeasible'
        target.write_text(json.dumps(content), encoding='utf-8')
    with pytest.raises(ValueError):
        r.status(path)


@pytest.mark.parametrize('filename', ['routing.json', 'session.json'])
def test_routing_marker_cannot_be_removed_alone(tmp_path, filename):
    path = tmp_path / 'marker'
    r.submit(path, partial().encode())
    if filename == 'routing.json':
        (path / filename).unlink()
    else:
        data = json.loads((path / filename).read_text())
        data.pop('routing_version')
        (path / filename).write_text(json.dumps(data))
    with pytest.raises((ValueError, OSError)):
        hitl.status(path)


def test_rejection(tmp_path):
    path = tmp_path / 'rejected'
    approved(path)
    hitl.reject(path, 'Wrong source, discard proposal')
    assert not hitl.status(path)['approval_valid']
    with pytest.raises(ValueError, match='approval'):
        r.solve(path)


def test_infeasibility_enumerated(tmp_path):
    original = source().replace('capacity: 12.', 'capacity: 0.')
    result = r.submit(tmp_path / 'infeasible', original.encode())['result']
    assert result['outcome'] == 'infeasible'
    assert result['verification'] is None and not result['optimality_proven']
    assert 'not independently certified' in result['infeasibility_evidence']


def test_timeout(tmp_path):
    result = r.submit(tmp_path / 'timeout', source().encode(), seconds=0)['result']
    assert result['outcome'] == 'budget_exhausted' and not result['optimality_proven']


def test_independent_verifier_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(hitl, 'verify', lambda *a: dict(valid=False, violations=['injected']))
    result = r.submit(tmp_path / 'failure', source().encode())['result']
    assert result['outcome'] == 'verification_error' and not result['optimality_proven']


def test_feasible_never_claimed_optimal(tmp_path, monkeypatch):
    real = hitl.solve_gap
    def feasible(*args):
        result = real(*args)
        return dict(result, raw_status='FEASIBLE', solver_proof=False)
    monkeypatch.setattr(hitl, 'solve_gap', feasible)
    result = r.submit(tmp_path / 'feasible', source().encode())['result']
    assert result['outcome'] == 'feasible' and not result['optimality_proven']


def test_resume_no_second_generation_or_solve(tmp_path, monkeypatch):
    for name, original in [('auto', source()), ('human', partial())]:
        path = tmp_path / name
        if name == 'auto': r.submit(path, original.encode())
        else:
            approved(path)
            r.solve(path)
        monkeypatch.setattr(hitl, 'solve_gap', lambda *a: pytest.fail('second solve'))
        monkeypatch.setattr(hitl.agent, 'ollama_chat', lambda *a: pytest.fail('second generation'))
        assert r.status(path)['result']
        assert r.solve(path)
        monkeypatch.undo()


@pytest.mark.parametrize('response', ['not HITL', hitl.FORMAT])
def test_single_llm_draft_never_approves(tmp_path, response):
    calls = []
    def client(payload, timeout):
        calls.append(payload)
        return dict(done=True, message=dict(content=response))
    path = tmp_path / 'llm'
    status = r.submit(path, partial().encode(), llm=True, client=client, admission=lambda: dict(allowed=True))
    assert len(calls) == 1
    assert not status['result'] and not status['hitl']['approval_valid']
    assert status['hitl']['approval'] is None
    assert json.loads((path / 'generation.json').read_text())['calls'] == 1
    with pytest.raises(ValueError):
        hitl.propose(path, partial().encode(), existing=True, client=client)


def test_ollama_absent_keeps_manual_draft(tmp_path):
    def unavailable(*args):
        raise ConnectionError('Ollama stopped')
    path = tmp_path / 'offline'
    result = r.submit(path, partial().encode(), llm=True, client=unavailable, admission=lambda: dict(allowed=True))
    assert result['routing']['branch'] == 'HITL' and not result['result']
    assert '?' in (path / 'proposal.txt').read_text()
    assert 'Ollama stopped' in (path / 'generation.json').read_text()


def test_uninterpreted_prose_blocks_even_perfect_llm_proposal(tmp_path):
    def client(*args):
        return dict(done=True, message=dict(content=r.manual_proposal(d.extract(source()))))
    output = r.submit(tmp_path / 'prose', (source() + '\nNote: an unknown condition applies.').encode(),
                      llm=True, client=client, admission=lambda: dict(allowed=True))
    assert output['hitl']['validation']['source_blockers']
    assert not output['hitl']['validation']['complete']


def test_cli_restart(tmp_path):
    path = tmp_path / 'cli'
    original = tmp_path / 'source.txt'
    original.write_text(source(), encoding='utf-8')
    def cli(*args):
        result = subprocess.run([sys.executable, '-B', '-m', 'or_agent', 'route', *map(str, args)],
                                capture_output=True, text=True, encoding='utf-8', cwd=ROOT,
                                env={**os.environ, 'PYTHONIOENCODING':'utf-8'})
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)
    cert = cli('analyze', original)['certificate']
    assert cert['status'] == 'VERIFIED_COMPLETE'
    initial = cli('submit', path, '--source', original)
    assert initial['result']['outcome'] == 'optimal'
    assert cli('status', path)['result'] == initial['result']
    assert cli('solve', path) == initial['result']
