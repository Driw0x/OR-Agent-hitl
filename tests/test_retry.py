"""Offline checks of abstention retries; fixture approvals are simulated."""

import json
import socket

import pytest

from or_agent import hitl, retry

SOURCE = '\n'.join(hitl.FORMAT.splitlines()[2:]) + '\n'
PARTIAL = hitl.FORMAT.replace('cost=8 euro', 'cost=? euro')
MORE_PARTIAL = PARTIAL.replace('cost=5 euro', 'cost=? euro')
ISSUE = hitl.FORMAT + 'Issue: I cannot reconstruct the complete problem.\n'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('offline retry test attempted network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


def execute(tmp_path, texts, *, source=SOURCE, max_retries=2, durations=None, total_seconds=45):
    clock, calls = Clock(), []
    def client(payload, timeout):
        index = len(calls)
        calls.append((payload, timeout))
        clock.value += durations[index] if durations else 1
        value = texts[index]
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, dict) else dict(done=True, done_reason='stop', message=dict(content=value),
            prompt_eval_count=100, eval_count=50)
    result = retry.run(tmp_path / 'run', source, client=client, admission=lambda _: dict(allowed=True),
        max_retries=max_retries, total_seconds=total_seconds, clock=clock)
    return result, calls


def test_recoverable_abstention():
    result = retry.diagnose(SOURCE, ISSUE)
    assert result['eligible'] and result['status'] == 'ABSTAIN'


@pytest.mark.parametrize('source,status', [
    ('The capacities have not been supplied.', 'NEEDS_CLARIFICATION'),
    ('The author does not know whether capacity is cumulative.', 'NEEDS_CLARIFICATION'),
    ('Also construct start times for the tasks.', 'UNSUPPORTED'),
    ('The values contradict each other.', 'INVALID'),
    (SOURCE + 'Cumulative capacity of Machine_A: 6 hour.\n', 'INVALID'),
])
def test_justified_abstention(tmp_path, source, status):
    result, calls = execute(tmp_path, [ISSUE], source=source)
    assert len(calls) == 1 and result['status'] == status


@pytest.mark.parametrize('texts,expected', [
    ([PARTIAL, hitl.FORMAT], 2),
    ([MORE_PARTIAL, PARTIAL, hitl.FORMAT], 3),
    ([MORE_PARTIAL, PARTIAL, ISSUE], 3),
])
def test_progress_and_call_ceiling(tmp_path, texts, expected):
    result, calls = execute(tmp_path, texts)
    assert len(calls) == expected
    assert result['status'] == ('READY_VERIFIED' if texts[-1] == hitl.FORMAT else 'CALL_BUDGET_EXHAUSTED')
    assert all(p['model'] == retry.agent.MODEL and p['options'] == retry.agent.OPTIONS for p, _ in calls)
    if result['status'] == 'READY_VERIFIED':
        assert result['verification']['valid'] and result['optimality_proven']
        assert result['solver']['objective'] == 12


def test_stop_on_repetition(tmp_path):
    result, calls = execute(tmp_path, [ISSUE, ISSUE.replace('cannot reconstruct', 'still cannot reconstruct')])
    assert len(calls) == 2 and result['reason'] == 'repeated_without_progress'


@pytest.mark.parametrize('max_retries', [0, 1, 2])
def test_exact_retry_budgets(tmp_path, max_retries):
    result, calls = execute(tmp_path, [MORE_PARTIAL, PARTIAL, ISSUE], max_retries=max_retries)
    assert len(calls) == max_retries + 1
    assert result['status'] == 'CALL_BUDGET_EXHAUSTED'


@pytest.mark.parametrize('value', [-1, 3, True, 0.5])
def test_invalid_retry_budget(tmp_path, value):
    with pytest.raises(ValueError, match='max_retries'):
        retry.run(tmp_path / 'run', SOURCE, max_retries=value)


@pytest.mark.parametrize('value', [0, -1, 46, True, float('nan')])
def test_invalid_time_budget(tmp_path, value):
    with pytest.raises(ValueError, match='budget'):
        retry.run(tmp_path / 'run', SOURCE, total_seconds=value)


def test_wrong_retry_rejected_by_source_locks(tmp_path):
    wrong = hitl.FORMAT.replace('cost=8 euro', 'cost=9 euro')
    result, calls = execute(tmp_path, [PARTIAL, wrong])
    assert len(calls) == 2 and result['reason'] == 'source_lock_violation'
    assert result['solver'] is None


def test_partial_source_locks(tmp_path):
    wrong = PARTIAL.replace('cost=5 euro', 'cost=9 euro')
    result, calls = execute(tmp_path, [wrong])
    assert len(calls) == 1 and result['reason'] == 'source_lock_violation'


def test_ready_unverified_never_solved(tmp_path, monkeypatch):
    monkeypatch.setattr(retry, 'solve', lambda *args: pytest.fail('unverified solve'))
    result, calls = execute(tmp_path, [hitl.FORMAT], source='A request in free prose with no recognized grammar.')
    assert len(calls) == 1 and result['status'] == 'READY_UNVERIFIED' and result['solver'] is None


@pytest.mark.parametrize('value,reason', [
    ('{"disposition":"clarify"}', 'invalid_format'),
    (dict(done=True, done_reason='length', message=dict(content=PARTIAL)), 'truncated'),
    (dict(done=False, message=dict(content=hitl.FORMAT)), 'uncertain_termination'),
    (OSError('transport failed'), 'transport_or_contract_error'),
    (TimeoutError('client deadline'), 'timeout'),
])
def test_technical_failures_do_not_retry(tmp_path, value, reason):
    result, calls = execute(tmp_path, [value])
    assert len(calls) == 1 and result['reason'] == reason and result['solver'] is None
    assert retry.prefix(tmp_path / 'run', 2)['reason'] == reason


def test_time_budget_exhausted_before_call(tmp_path):
    result, calls = execute(tmp_path, [], total_seconds=5)
    assert not calls and result['status'] == 'TIME_BUDGET_EXHAUSTED'


def test_remaining_time_bounds_timeout(tmp_path):
    result, calls = execute(tmp_path, [PARTIAL], durations=[36])
    assert len(calls) == 1 and result['status'] == 'TIME_BUDGET_EXHAUSTED'
    assert result['automatic_seconds'] == 36 and calls[0][1] == 44.75


def test_managed_deadline_overrun(tmp_path):
    result, calls = execute(tmp_path, [hitl.FORMAT], durations=[46])
    assert len(calls) == 1 and result['status'] == 'TIME_BUDGET_EXHAUSTED' and result['solver'] is None


def test_no_retry_after_terminal(tmp_path):
    result, calls = execute(tmp_path, [hitl.FORMAT])
    assert len(calls) == 1 and result['status'] == 'READY_VERIFIED'


def test_resume_no_duplicate_and_fingerprints(tmp_path):
    expected, _ = execute(tmp_path, [PARTIAL, hitl.FORMAT])
    before = (tmp_path / 'run/events.jsonl').read_bytes()
    actual = retry.run(tmp_path / 'run', SOURCE, client=lambda *args: pytest.fail('duplicate request'))
    assert actual == expected and (tmp_path / 'run/events.jsonl').read_bytes() == before
    with pytest.raises(ValueError, match='mismatch'):
        retry.run(tmp_path / 'run', SOURCE + '\n')


def test_resume_after_completed_attempt(tmp_path):
    execute(tmp_path, [PARTIAL], max_retries=0)
    path = tmp_path / 'run'
    # Model an interruption after fsynced attempt and before controller finish.
    lines = (path / 'events.jsonl').read_bytes().splitlines(keepends=True)
    (path / 'events.jsonl').write_bytes(b''.join(lines[:-1]))
    result = retry.run(path, SOURCE, max_retries=0, client=lambda *args: pytest.fail('duplicate request'))
    assert result['calls'] == 1 and result['status'] == 'CALL_BUDGET_EXHAUSTED'


def test_orphan_request_never_replayed(tmp_path):
    execute(tmp_path, [PARTIAL], max_retries=0)
    path = tmp_path / 'run'
    lines = (path / 'events.jsonl').read_bytes().splitlines(keepends=True)
    (path / 'events.jsonl').write_bytes(b''.join(lines[:2]))
    with pytest.raises(ValueError, match='unfinished'):
        retry.run(path, SOURCE, max_retries=0, client=lambda *args: pytest.fail('duplicate request'))


def test_journal_tamper(tmp_path):
    execute(tmp_path, [hitl.FORMAT])
    path = tmp_path / 'run/events.jsonl'
    path.write_bytes(path.read_bytes().replace(b'whole_source_verified', b'WHOLE_source_verified'))
    with pytest.raises(ValueError, match='fingerprint'):
        retry.journal(path.parent)


def test_resource_refusal(tmp_path):
    result = retry.run(tmp_path / 'run', SOURCE, admission=lambda _: dict(allowed=False),
                       client=lambda *args: pytest.fail('refused request'))
    assert result['calls'] == 0 and result['reason'] == 'resource_refused'


def test_feedback_has_no_oracle(tmp_path):
    reference = tmp_path / 'reference.json'
    reference.write_text('{"oracle":"DO_NOT_READ_123"}', encoding='utf-8')
    execute(tmp_path, [PARTIAL, hitl.FORMAT])
    rows = retry.attempts(tmp_path / 'run')
    assert rows[0]['request'] == hitl.request(SOURCE)
    content = json.dumps(rows[1]['request'])
    assert 'DO_NOT_READ_123' not in content and 'Diagnostics are observations' in content
    assert reference.read_text() == '{"oracle":"DO_NOT_READ_123"}'


def test_prefixes_reuse_saved_calls(tmp_path, monkeypatch):
    execute(tmp_path, [MORE_PARTIAL, PARTIAL, hitl.FORMAT])
    monkeypatch.setattr(retry, 'solve', lambda *args: pytest.fail('offline replay solve'))
    results = [retry.prefix(tmp_path / 'run', budget) for budget in (0, 1, 2)]
    assert [len(r['attempts']) for r in results] == [1, 2, 3]
    assert [r['status'] for r in results] == ['CALL_BUDGET_EXHAUSTED', 'CALL_BUDGET_EXHAUSTED', 'READY_VERIFIED']
    assert [r['automatic_seconds'] for r in results] == [1, 2, 3]


def test_fallback_reuses_trace_and_requires_approval(tmp_path):
    execute(tmp_path, [PARTIAL, hitl.FORMAT], source='Free prose, preserved verbatim.')
    path, session = tmp_path / 'run', tmp_path / 'human'
    result = retry.fallback(path, session)
    assert result['approval'] is None and result['state'] == 'NEEDS_REVIEW'
    assert (session / 'proposal.txt').read_text() == hitl.FORMAT
    assert (session / 'retry-events.jsonl').read_bytes() == (path / 'events.jsonl').read_bytes()
    assert retry.fallback(path, session)['run_id'] == result['run_id']
    with pytest.raises(ValueError, match='approval'):
        hitl.solve(session)
    review = hitl.review(session)
    hitl.approve(session, review['review_token'], 'APPROVE:' + review['review_token'], simulated=True)
    solved = hitl.solve(session)
    assert solved['verification']['valid'] and solved['optimality_proven']
    (session / 'proposal.txt').write_text(PARTIAL)
    with pytest.raises(ValueError, match='approval'):
        hitl.solve(session)


def test_infeasibility_on_verified_source(tmp_path):
    raw = hitl.FORMAT.replace('Machine_A: 5 hour', 'Machine_A: 0 hour').replace('Machine_B: 4 hour', 'Machine_B: 0 hour')
    source = '\n'.join(raw.splitlines()[2:]) + '\n'
    result, _ = execute(tmp_path, [raw], source=source)
    assert result['solver']['raw_status'] == 'INFEASIBLE' and not result['optimality_proven']


def test_independent_verifier_failure_not_success(tmp_path, monkeypatch):
    monkeypatch.setattr(retry, 'verify', lambda *args: dict(valid=False))
    result, _ = execute(tmp_path, [hitl.FORMAT])
    assert not result['optimality_proven'] and not result['verification']['valid']


def test_shared_campaign_lock(tmp_path):
    with retry.campaign_lock():
        with pytest.raises(ValueError, match='owns the experiment lock'):
            retry.run(tmp_path / 'run', SOURCE)


def test_wrong_prose_retry_stays_unverified(tmp_path):
    wrong = hitl.FORMAT.replace('cost=8 euro', 'cost=99 euro')
    result, calls = execute(tmp_path, [PARTIAL, wrong], source='Free prose with no deterministic proof.')
    assert len(calls) == 2 and result['status'] == 'READY_UNVERIFIED' and result['solver'] is None


def test_uncertain_completed_run_cannot_generate_again(tmp_path):
    expected, _ = execute(tmp_path, [dict(done=False, message=dict(content=PARTIAL))])
    actual = retry.run(tmp_path / 'run', SOURCE, client=lambda *args: pytest.fail('uncertain retry'))
    assert actual == expected


def test_duplicate_start_refused(tmp_path):
    execute(tmp_path, [PARTIAL], max_retries=0)
    path = tmp_path / 'run'
    retry.append(path, kind='start', index=0, request=hitl.request(SOURCE))
    with pytest.raises(ValueError, match='duplicate'):
        retry.attempts(path)


def test_sessions_isolated_and_no_auto_approval(tmp_path):
    execute(tmp_path, [hitl.FORMAT])
    first = retry.fallback(tmp_path / 'run', tmp_path / 'first')
    second = retry.fallback(tmp_path / 'run', tmp_path / 'second')
    assert first['run_id'] != second['run_id']
    assert first['approval'] is second['approval'] is None


def test_non_text_response_has_reviewable_fallback(tmp_path):
    result, calls = execute(tmp_path, [dict(done=True, message=dict(content=None))])
    assert len(calls) == 1 and result['reason'] == 'transport_or_contract_error'
    assert retry.fallback(tmp_path / 'run', tmp_path / 'human')['state'] == 'NEEDS_CLARIFICATION'


def test_no_retry_from_missing_proposal_cell_proves_source_missing():
    # A missing proposal cell is recoverable even when the source is unrecognized.
    result = retry.diagnose('Unrecognized source; completeness remains unknown.', PARTIAL)
    assert result['eligible'] and result['status'] == 'ABSTAIN'
