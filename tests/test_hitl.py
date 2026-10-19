"""Deterministic HITL checks. All approvals here are SIMULATED, never human evidence."""

import json
from pathlib import Path
import socket
import sys

import pytest

from or_agent import cli, hitl
from or_agent.models import Gap


PROBLEM = dict(tasks=['Cut', 'Assemble'], resources=['Machine_A', 'Machine_B'],
    costs=[[8, 5], [7, 6]], consumption=[[2, 3], [3, 2]], capacities=[5, 4],
    cost_unit='euro', resource_unit='hour')


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('deterministic HITL test attempted network access')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


def draft(path, text=hitl.FORMAT, source=b'Original free prose, kept exactly.\r\n'):
    hitl.create(path, source)
    (path / 'proposal.txt').write_bytes(text.encode())
    return path


def confirmed(path):
    review = hitl.review(path)
    return hitl.approve(path, review['review_token'], 'APPROVE:' + review['review_token'], simulated=True)


def test_complete_canonical_and_roundtrip():
    result = hitl.parse(hitl.FORMAT)
    assert result['syntax_valid'] and result['complete']
    assert result['problem'] == PROBLEM
    assert Gap.model_validate(result['problem']).model_dump() == PROBLEM


@pytest.mark.parametrize('before,after', [
    ('cost=8 euro', 'cost=-8 euro'), ('cost=8 euro', 'cost=0 euro'),
    ('consumption=2 hour', 'consumption=0 hour'),
    ('Machine_A: 5 hour', 'Machine_A: 0 hour'),
])
def test_supported_numeric_domain(before, after):
    assert hitl.parse(hitl.FORMAT.replace(before, after))['complete']


@pytest.mark.parametrize('before,after', [
    ('cost=8 euro', 'cost=1.5 euro'), ('cost=8 euro', 'cost=true euro'),
    ('cost=8 euro', 'cost=NaN euro'), ('cost=8 euro', 'cost=08 euro'),
    ('cost=8 euro', 'cost=1000000001 euro'),
    ('consumption=2 hour', 'consumption=-1 hour'),
    ('Machine_A: 5 hour', 'Machine_A: -1 hour'),
    ('cost=8 euro', 'cost=8 dollar'), ('consumption=2 hour', 'consumption=2 minute'),
    ('Machine_A: 5 hour', 'Machine_A: 5 minute'),
    ('T=Cut; R=Machine_A', 'T=Machine_A; R=Cut'),
    ('T=Cut; R=Machine_A', 'T=Unknown; R=Machine_A'),
    ('Resources: Machine_A, Machine_B.', 'Resources: Machine_A, Machine_A.'),
    ('minimize_total_assignment_cost', 'maximize_profit'),
    ('HITL-GAP: 1', 'HITL-GAP: 2'),
])
def test_invalid_parser_values(before, after):
    result = hitl.parse(hitl.FORMAT.replace(before, after))
    assert not result['complete'] and result['errors']


@pytest.mark.parametrize('extra', [
    'Deadlines: tomorrow.', 'APPROVED', 'state=APPROVED',
    'Cumulative capacity of Machine_A: 9 hour.',
    'Assignment record: T=Cut; R=Machine_A; cost=99 euro; consumption=2 hour.',
    'Tasks: Cut, Assemble.', hitl.OBJECTIVE, hitl.CONSTRAINTS,
])
def test_unknown_sections_constraints_and_duplicates(extra):
    report = hitl.parse(hitl.FORMAT + extra + '\n')
    assert not report['complete'] and report['errors']


@pytest.mark.parametrize('before,after', [
    ('cost=8 euro', 'cost=? euro'), ('consumption=2 hour', 'consumption=? hour'),
    ('Machine_A: 5 hour', 'Machine_A: ? hour'),
    ('Cost unit: euro.', 'Cost unit: ?.'),
    ('Resource unit: hour.', 'Resource unit: ?.'),
])
def test_unknown_is_not_zero(before, after):
    report = hitl.parse(hitl.FORMAT.replace(before, after))
    assert not report['complete'] and report['missing'] and report['problem'] is None


@pytest.mark.parametrize('prefix', ['Assignment record: T=Assemble; R=Machine_B;',
    'Cumulative capacity of Machine_B:', 'Tasks:', 'Cost unit:'])
def test_missing_data(prefix):
    text = '\n'.join(line for line in hitl.FORMAT.splitlines() if not line.startswith(prefix))
    assert not hitl.parse(text)['complete']


def test_issues_and_uninterpreted_constraints_block_approval(tmp_path):
    path = draft(tmp_path / 'session', hitl.FORMAT + 'Issue: Cut must finish before noon; unsupported deadline.\n')
    report = hitl.review(path)
    assert report['state'] == 'NEEDS_CLARIFICATION'
    assert report['validation']['issues']
    with pytest.raises(ValueError, match='incomplete'):
        confirmed(path)


def test_draft_and_reopen(tmp_path):
    path = tmp_path / 'session'
    initial = hitl.create(path, b'\xef\xbb\xbfOriginal\r\n')
    assert initial.state == 'DRAFT'
    assert hitl.status(path)['state'] == 'DRAFT'
    assert hitl.load(path)[1] == b'\xef\xbb\xbfOriginal\r\n'
    assert hitl.validate(path)['state'] == 'NEEDS_CLARIFICATION'


def test_explicit_review_and_confirmation_required(tmp_path):
    path = draft(tmp_path / 'session')
    report = hitl.status(path)
    with pytest.raises(ValueError, match='explicit confirmation'):
        hitl.approve(path, report['review_token'], 'APPROVE:' + report['review_token'], simulated=True)
    review = hitl.review(path)
    for token, confirmation in [('wrong', 'APPROVE:' + review['review_token']),
                                (review['review_token'], 'yes'), (review['review_token'], '')]:
        with pytest.raises(ValueError, match='explicit confirmation'):
            hitl.approve(path, token, confirmation, simulated=True)
    result = confirmed(path)
    assert result['state'] == 'APPROVED' and result['approval_valid']
    assert result['approval']['actor'] == 'simulated_reviewer'
    assert result['approval']['confirmed_at']
    assert json.loads((path / 'canonical.json').read_text()) == PROBLEM


@pytest.mark.parametrize('state', ['DRAFT', 'NEEDS_REVIEW', 'NEEDS_CLARIFICATION', 'REJECTED'])
def test_unapproved_never_reaches_solver(tmp_path, monkeypatch, state):
    path = draft(tmp_path / 'session')
    session, _ = hitl.load(path)
    session.state = state
    hitl.event(path, session, 'simulated_state')
    monkeypatch.setattr(hitl, 'solve_gap', lambda *a: pytest.fail('unapproved draft reached CP-SAT'))
    with pytest.raises(ValueError, match='approval required'):
        hitl.solve(path)


def test_rejected_is_terminal(tmp_path, monkeypatch):
    path = draft(tmp_path / 'session')
    confirmed(path)
    assert hitl.reject(path, 'wrong associations')['state'] == 'REJECTED'
    monkeypatch.setattr(hitl, 'solve_gap', lambda *a: pytest.fail('rejected proposal reached CP-SAT'))
    with pytest.raises(ValueError): hitl.solve(path)
    with pytest.raises(ValueError, match='rejected'): confirmed(path)
    with pytest.raises(ValueError): hitl.reject(path, '')


@pytest.mark.parametrize('before,after', [
    ('cost=8 euro', 'cost=9 euro'), ('consumption=2 hour', 'consumption=1 hour'),
    ('Machine_A: 5 hour', 'Machine_A: 6 hour'),
    ('T=Cut; R=Machine_A', 'T=Assemble; R=Machine_A'),
    ('Cut', 'Cut2'), ('Machine_A', 'Machine_C'),
    ('euro', 'credit'), ('hour', 'minute'),
    ('minimize_total_assignment_cost', 'maximize_profit'),
    (hitl.CONSTRAINTS, 'Allow optional tasks.'),
    ('HITL-GAP: 1\n', 'HITL-GAP: 1\n\n'),
])
def test_every_change_invalidates_approval(tmp_path, monkeypatch, before, after):
    path = draft(tmp_path / 'session')
    confirmed(path)
    (path / 'proposal.txt').write_text(hitl.FORMAT.replace(before, after), encoding='utf-8')
    monkeypatch.setattr(hitl, 'solve_gap', lambda *a: pytest.fail('stale approval reached solver'))
    with pytest.raises(ValueError): hitl.solve(path)
    assert not hitl.status(path)['approval_valid']
    assert any(e['action'] == 'approval_invalidated' for e in hitl.load(path)[0].events)


@pytest.mark.parametrize('filename', ['original.txt', 'canonical.json', 'approved_proposal', 'approval_evidence'])
def test_session_evidence_tampering_fails_closed(tmp_path, monkeypatch, filename):
    path = draft(tmp_path / 'session')
    approval = confirmed(path)
    target = path / filename
    if filename in ('approved_proposal', 'approval_evidence'):
        target = path / 'approved' / approval['review_token'] / ('proposal.txt' if filename == 'approved_proposal' else 'approval.json')
    target.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(hitl, 'solve_gap', lambda *a: pytest.fail('tampered evidence reached solver'))
    with pytest.raises(ValueError): hitl.solve(path)


@pytest.mark.parametrize('field,value', [('state', 'APPROVED'), ('grammar', 'other'),
    ('approval', {'state': 'APPROVED'}), ('auto_approved', True)])
def test_state_file_cannot_replace_approval(tmp_path, monkeypatch, field, value):
    path = draft(tmp_path / 'session')
    session = json.loads((path / 'session.json').read_text())
    session[field] = value
    (path / 'session.json').write_text(json.dumps(session), encoding='utf-8')
    monkeypatch.setattr(hitl, 'solve_gap', lambda *a: pytest.fail('forged state reached solver'))
    with pytest.raises(ValueError): hitl.solve(path)


def test_review_receipt_expires_and_new_approval_works(tmp_path):
    path = draft(tmp_path / 'session')
    first = confirmed(path)
    (path / 'proposal.txt').write_text(hitl.FORMAT.replace('cost=8 euro', 'cost=-8 euro'), encoding='utf-8')
    with pytest.raises(ValueError):
        hitl.approve(path, first['review_token'], 'APPROVE:' + first['review_token'], simulated=True)
    second = confirmed(path)
    assert second['review_token'] != first['review_token']
    assert len(list((path / 'approved').iterdir())) == 2
    result = hitl.solve(path)
    assert result['approved_problem']['costs'][0][0] == -8
    assert result['solver']['objective'] == -3 and result['verification']['valid']


def test_sessions_isolated_and_restartable(tmp_path):
    first, second = draft(tmp_path / 'first'), draft(tmp_path / 'second')
    a = confirmed(first)
    b = hitl.review(second)
    assert a['run_id'] != b['run_id'] and a['review_token'] != b['review_token']
    with pytest.raises(ValueError):
        hitl.approve(second, a['review_token'], 'APPROVE:' + a['review_token'], simulated=True)
    # Reload from disk, with no in-memory session object.
    assert hitl.solve(Path(str(first)))['verification']['valid']
    assert not hitl.status(second)['approval_valid']


def test_optimal_reconstruction_and_capacity(tmp_path):
    path = draft(tmp_path / 'session')
    confirmed(path)
    result = hitl.solve(path)
    assert result['outcome'] == 'optimal' and result['optimality_proven']
    assert result['verification']['valid'] and result['verification']['recomputed_objective'] == 12
    assert result['approved_problem'] == PROBLEM
    assert result['fingerprints'] == hitl.status(path)['fingerprints']
    assert hitl.status(path)['state'] == 'SOLVED'
    assert result['verification']['loads'] == {'Machine_A': 3, 'Machine_B': 3}


def test_multiple_optima(tmp_path):
    text = hitl.FORMAT
    for cost in (8, 5, 7, 6): text = text.replace('cost=' + str(cost) + ' euro', 'cost=1 euro')
    path = draft(tmp_path / 'session', text)
    confirmed(path)
    result = hitl.solve(path)
    assert result['verification']['valid'] and result['solver']['objective'] == 2


def test_infeasibility_is_solver_proof_not_checker_proof(tmp_path):
    text = hitl.FORMAT.replace('Machine_A: 5 hour', 'Machine_A: 0 hour').replace('Machine_B: 4 hour', 'Machine_B: 0 hour')
    path = draft(tmp_path / 'session', text)
    confirmed(path)
    result = hitl.solve(path)
    assert result['outcome'] == 'infeasible' and result['solver']['raw_status'] == 'INFEASIBLE'
    assert result['verification'] is None and not result['optimality_proven']
    assert 'not independently certified' in result['infeasibility_evidence']


def test_actual_zero_budget(tmp_path):
    path = draft(tmp_path / 'session')
    confirmed(path)
    result = hitl.solve(path, 0)
    assert result['solver']['raw_status'] == 'UNKNOWN'
    assert result['outcome'] == 'budget_exhausted' and result['verification'] is None


@pytest.mark.parametrize('seconds', [float('inf'), float('nan'), -1, 3601, True])
def test_solver_budgets(tmp_path, seconds):
    with pytest.raises(ValueError): hitl.solve(tmp_path, seconds)


def test_negative_independent_check(tmp_path, monkeypatch):
    path = draft(tmp_path / 'session')
    confirmed(path)
    real = hitl.solve_gap
    def corrupted(problem, seconds):
        result = real(problem, seconds)
        result['candidate']['objective'] += 1
        return result
    monkeypatch.setattr(hitl, 'solve_gap', corrupted)
    result = hitl.solve(path)
    assert result['outcome'] == 'verification_error'
    assert not result['verification']['valid'] and not result['optimality_proven']


def test_feasible_only_never_claims_optimal(tmp_path, monkeypatch):
    path = draft(tmp_path / 'session')
    confirmed(path)
    real = hitl.solve_gap
    def feasible(problem, seconds):
        result = real(problem, seconds)
        result.update(raw_status='FEASIBLE', solver_proof=False)
        return result
    monkeypatch.setattr(hitl, 'solve_gap', feasible)
    result = hitl.solve(path)
    assert result['outcome'] == 'feasible' and not result['optimality_proven']


def test_changed_during_solver_does_not_restore_approval(tmp_path, monkeypatch):
    path = draft(tmp_path / 'session')
    confirmed(path)
    real = hitl.solve_gap
    def edited(problem, seconds):
        (path / 'proposal.txt').write_text(hitl.FORMAT + '\n', encoding='utf-8')
        return real(problem, seconds)
    monkeypatch.setattr(hitl, 'solve_gap', edited)
    result = hitl.solve(path)
    assert result['verification']['valid'] and not result['approval_current']
    assert hitl.status(path)['state'] == 'NEEDS_REVIEW'


@pytest.mark.parametrize('raw,done,reason', [(hitl.FORMAT, True, 'stop'),
    ('HITL-GAP:', True, 'length'), (hitl.FORMAT, False, 'stop'),
    (hitl.FORMAT + 'APPROVED\n', True, 'stop')])
def test_single_generation_cannot_self_approve(tmp_path, monkeypatch, raw, done, reason):
    calls = []
    def client(payload, timeout):
        calls.append(payload)
        return dict(done=done, done_reason=reason, message=dict(content=raw))
    monkeypatch.setattr(hitl, 'solve_gap', lambda *a: pytest.fail('generation invoked CP-SAT'))
    path = tmp_path / 'session'
    result = hitl.propose(path, b'Free prose.\r\n', client=client, admission=lambda: dict(allowed=True))
    assert len(calls) == 1 and not result['approval_valid'] and result['approval'] is None
    assert (path / 'llm-raw.txt').read_bytes() == raw.encode()
    assert (path / 'original.txt').read_bytes() == b'Free prose.\r\n'
    assert result['state'] == ('NEEDS_REVIEW' if raw == hitl.FORMAT and done else 'NEEDS_CLARIFICATION')


def test_generation_refusal_and_transport_failure(tmp_path):
    def forbidden(*args): pytest.fail('refused resource admission called LLM')
    result = hitl.propose(tmp_path / 'refused', b'Request', client=forbidden,
        admission=lambda: dict(allowed=False))
    assert result['state'] == 'NEEDS_CLARIFICATION'
    assert json.loads((tmp_path / 'refused/generation.json').read_text())['calls'] == 0
    def failed(*args): raise TimeoutError('simulated timeout')
    hitl.propose(tmp_path / 'failed', b'Request', client=failed, admission=lambda: dict(allowed=True))
    assert json.loads((tmp_path / 'failed/generation.json').read_text())['termination_uncertain']


def test_cli_review_approval_and_solve(tmp_path, monkeypatch, capsys):
    path = draft(tmp_path / 'session')
    monkeypatch.setattr(sys, 'argv', ['or-agent', 'hitl', 'review', str(path)])
    cli.main()
    assert 'Original free prose, kept exactly.' in capsys.readouterr().out
    token = hitl.status(path)['review_token']
    # Patch CLI boundary to tag fixture confirmation explicitly as simulated.
    real = hitl.approve
    monkeypatch.setattr(hitl, 'approve', lambda p, t, c: real(p, t, c, simulated=True))
    monkeypatch.setattr(sys, 'argv', ['or-agent', 'hitl', 'approve', str(path), '--review-token', token, '--confirm', 'APPROVE:' + token])
    cli.main()
    assert json.loads(capsys.readouterr().out)['approval_valid']
    monkeypatch.setattr(sys, 'argv', ['or-agent', 'hitl', 'solve', str(path)])
    cli.main()
    assert json.loads(capsys.readouterr().out)['verification']['valid']


def offline_scenarios(root):
    """Eight reproducible scenarios, with all reconstructed JSON and transitions."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    results = []
    for name in ('correct', 'cost_correction', 'consumption_correction', 'missing',
                 'rejection', 'edited_after_approval', 'infeasible', 'invalid_output'):
        text = hitl.FORMAT
        if name == 'cost_correction':
            text = text.replace('cost=8 euro', 'cost=5 euro').replace('cost=5 euro; consumption=3', 'cost=8 euro; consumption=3')
        if name == 'consumption_correction':
            text = text.replace('cost=7 euro; consumption=3', 'cost=7 euro; consumption=2').replace('cost=6 euro; consumption=2', 'cost=6 euro; consumption=3')
        if name == 'missing': text = text.replace('cost=8 euro', 'cost=? euro')
        if name == 'infeasible':
            text = text.replace('Machine_A: 5 hour', 'Machine_A: 0 hour').replace('Machine_B: 4 hour', 'Machine_B: 0 hour')
        if name == 'invalid_output': text = 'HITL-GAP: 1\nTasks: Cut,'
        path = root / name
        source = ('Assign Cut and Assemble, each exactly once, to Machine_A or Machine_B. '
            'Minimize total cost in euro, obey cumulative capacities in hour; no other constraints. '
            'Machine_A costs 8 for Cut and 5 for Assemble; consumptions are 2 and 3 hour. '
            'Machine_B costs 7 for Cut and 6 for Assemble; consumptions are 3 and 2 hour. '
            + ('Both capacities are 0 hour.' if name == 'infeasible' else 'Capacities are 5 hour for Machine_A and 4 hour for Machine_B.'))
        result = hitl.propose(path, source.encode(),
            client=lambda payload, timeout: dict(done=True, message=dict(content=text)), admission=lambda: dict(allowed=True))
        row = dict(scenario=name, actor='simulated_reviewer', initial=result, blocked=[], result=None)
        if name in ('missing', 'invalid_output'):
            try: confirmed(path)
            except ValueError as error: row['blocked'].append(str(error))
        if name in ('cost_correction', 'consumption_correction', 'missing'):
            (path / 'proposal.txt').write_bytes(hitl.FORMAT.encode())
            row['corrected'] = hitl.validate(path)
        if name == 'rejection':
            row['rejected'] = hitl.reject(path, 'Simulated reviewer rejects formulation')
            try: hitl.solve(path)
            except ValueError as error: row['blocked'].append(str(error))
        elif name != 'invalid_output':
            row['approved'] = confirmed(path)
            if name == 'edited_after_approval':
                (path / 'proposal.txt').write_bytes((hitl.FORMAT + '\n').encode())
                try: hitl.solve(path)
                except ValueError as error: row['blocked'].append(str(error))
            else:
                row['result'] = hitl.solve(path)
                assert row['result']['outcome'] == ('infeasible' if name == 'infeasible' else 'optimal')
        row['final'] = hitl.status(path)
        row['events'] = hitl.load(path)[0].events
        results.append(row)
    return dict(OFFLINE_VALIDATION='PASSED', real_human_approvals=0, real_ollama_calls=0, scenarios=results)


def test_eight_offline_scenarios(tmp_path):
    result = offline_scenarios(tmp_path / 'scenarios')
    assert len(result['scenarios']) == 8
    assert sum(row['result'] is not None for row in result['scenarios']) == 5
    assert all(row['result']['verification']['valid'] for row in result['scenarios'] if row['result'] and row['scenario'] != 'infeasible')


def test_parser_refuses_oversized_cross_product():
    text = '\n'.join([hitl.HEADER, hitl.OBJECTIVE, hitl.CONSTRAINTS,
        'Tasks: ' + ', '.join('T' + str(i) for i in range(101)) + '.',
        'Resources: ' + ', '.join('R' + str(i) for i in range(101)) + '.'])
    assert 'at most 10000 task-resource pairs' in hitl.parse(text)['errors']


@pytest.mark.parametrize('before,after', [
    ('cost=8 euro; consumption=2 hour', 'cost=? euro; consumption=-2 hour'),
    ('cost=8 euro; consumption=2 hour', 'cost=1000000001 euro; consumption=? hour'),
    ('cost=8 euro; consumption=2 hour', 'cost=? dollar; consumption=2 hour'),
])
def test_partial_known_values_still_validated(before, after):
    report = hitl.parse(hitl.FORMAT.replace(before, after))
    assert not report['syntax_valid'] and report['errors']


def test_resume_interrupted_approval(tmp_path):
    path = draft(tmp_path / 'session')
    review = hitl.review(path)
    revision = path / 'approved' / review['review_token']
    revision.mkdir(parents=True)
    (revision / 'original.txt').write_bytes((path / 'original.txt').read_bytes())
    assert confirmed(path)['approval_valid']
    assert hitl.solve(path)['verification']['valid']


def test_edited_review_receipt_cannot_replace_review(tmp_path):
    path = draft(tmp_path / 'session')
    token = hitl.status(path)['review_token']
    session, _ = hitl.load(path)
    session.review_token = token
    hitl.event(path, session, 'edited_receipt')
    with pytest.raises(ValueError, match='explicit confirmation'):
        hitl.approve(path, token, 'APPROVE:' + token, simulated=True)
