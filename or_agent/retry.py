"""Experimental abstention retries over HITL proposals; never changes standard generation."""

import json
import math
import os
from pathlib import Path
import re
import time

from . import agent, deterministic, hitl
from .crosscheck import gap_facts
from .extended_evaluate import campaign_lock
from .models import Gap, load_json
from .resources import WaitBudget, gate
from .solver import solve
from .verify import verify

MIN_CALL_SECONDS = 10.0
DECISION_RESERVE = 0.25
POLICY = 'abstention_retry_v1'


def source_check(source):
    """Only explicit source evidence can justify a terminal source disposition.

    These narrow literal cues complement deterministic extraction; absence of a cue proves nothing.
    A proposal's missing cell alone never proves the source lacks that cell.
    """
    report = deterministic.extract(source)
    if report['contradictions']:
        return report, 'INVALID', 'source_contradiction'
    lower = source.lower()
    if re.search(r'\b(?:scheduling constraint is mandatory|routing is required|construct start times)\b', lower):
        return report, 'UNSUPPORTED', 'explicit_unsupported_constraint'
    if 'values contradict each other' in lower:
        return report, 'INVALID', 'explicit_source_contradiction'
    if re.search(r'\b(?:have not been supplied|has not been supplied|does not know whether)\b', lower):
        return report, 'NEEDS_CLARIFICATION', 'explicit_missing_ambiguous_or_conflicting_source'
    return report, None, None


def diagnose(source, raw):
    parsed = hitl.parse(raw)
    report, terminal, reason = source_check(source)
    result = dict(status='ABSTAIN', reason='recoverable_incomplete_proposal', eligible=False,
                  validation=parsed, locks=None, source_proof=report['complete_proof'])
    if terminal:
        result.update(status=terminal, reason=reason)
        return result
    if not parsed['syntax_valid']:
        result.update(reason='invalid_format', technical='invalid_format')
        return result
    proposed = deterministic.extract(raw)
    source_facts = {(f['field'], f['resource'], f['task']): (f['value'], f['unit'])
                    for f in report['facts'] if f['verification'] == 'verified'}
    differences = [f for f in proposed['facts'] if
                   (f['field'], f['resource'], f['task']) in source_facts and
                   (f['value'], f['unit']) != source_facts[(f['field'], f['resource'], f['task'])]]
    differences += [dict(field=k, supplied=v) for k, v in proposed['established'].items()
                    if k in report['established'] and
                    (set(v) != set(report['established'][k]) if isinstance(v, list)
                     else v != report['established'][k])]
    if parsed['complete']:
        result['locks'] = deterministic.check_locks(source, parsed['problem'])
        differences += result['locks']['differences']
    if differences:
        result.update(reason='source_lock_violation', violations=differences)
    elif parsed['complete']:
        verified = report['complete_problem'] is not None and result['locks']['accepted']
        result.update(status='READY_VERIFIED' if verified else 'READY_UNVERIFIED',
                      reason='whole_source_verified' if verified else 'source_fidelity_unassessed')
    else:
        result['eligible'] = True
    return result


def signature(raw):
    """Ignore cosmetic issue rewording; compare substantive established data."""
    report = deterministic.extract(raw)
    facts = sorted((f['field'], f['resource'], f['task'] or '', f['value'], f['unit'] or '')
                   for f in report['facts'])
    return hitl.sha(hitl.encoded([report['established'], facts]))


def request(source, previous=None):
    payload = hitl.request(source)
    if previous is not None:
        diagnosis = previous['diagnosis']
        feedback = dict(errors=diagnosis['validation']['errors'], missing=diagnosis['validation']['missing'],
                        issues=diagnosis['validation']['issues'])
        payload['messages'] += [dict(role='assistant', content=previous['raw']), dict(role='user', content=
            'Retry only the incomplete proposal using the ORIGINAL source above. '
            'Return the same complete HITL-GAP line format. Check every labelled pair. '
            'Alternative assignments are not simultaneous consumption. '
            'Preserve source facts, identifiers, units and all constraints. '
            'Never invent missing data. Retain ? and Issue if clarification is essential. '
            'Diagnostics are observations about your proposal, not new source data.\n' +
            json.dumps(feedback, ensure_ascii=False, separators=(',', ':')))]
    return payload


def journal(path):
    file = Path(path) / 'events.jsonl'
    rows, previous = [], None
    if file.exists():
        for line in file.read_text(encoding='utf-8').splitlines():
            row = load_json(line)
            digest = row.pop('sha256')
            if row['previous_sha256'] != previous or digest != hitl.sha(hitl.encoded(row)):
                raise ValueError('retry journal fingerprint mismatch')
            row['sha256'] = digest
            rows.append(row)
            previous = digest
    return rows


def append(path, **row):
    rows = journal(path)
    row.update(previous_sha256=rows[-1]['sha256'] if rows else None)
    row['sha256'] = hitl.sha(hitl.encoded(row))
    with (Path(path) / 'events.jsonl').open('a', encoding='utf-8', newline='\n') as output:
        output.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
        output.flush()
        os.fsync(output.fileno())


def attempts(path):
    rows = journal(path)
    starts = [r for r in rows if r['kind'] == 'start']
    results = [r['attempt'] for r in rows if r['kind'] == 'attempt']
    if [r['index'] for r in starts] != list(range(len(starts))) or len(starts) > 3:
        raise ValueError('duplicate or invalid retry starts')
    if len(starts) != len(results) or [r['index'] for r in results] != list(range(len(results))):
        raise ValueError('unfinished request; termination uncertain, no automatic replay')
    if any(r['termination_uncertain'] for r in results):
        # A finished uncertain run can be inspected, but never continued.
        if not rows or rows[-1]['kind'] != 'finish':
            raise ValueError('request termination uncertain; no retry')
    return results


def fallback(path, destination, index=-1):
    """Reuse a saved attempt. This function has no LLM client or approval path."""
    path, destination = Path(path), Path(destination)
    original = (path / 'original.txt').read_bytes()
    meta = load_json((path / 'config.json').read_text(encoding='utf-8'))
    if hitl.sha(original) != meta['source_sha256']:
        raise ValueError('retry source fingerprint mismatch')
    rows = attempts(path)
    selected = rows[index] if rows else None
    proposal = selected['raw'] if selected and selected['raw'] is not None else ''
    if selected and (selected['error'] or selected['termination_uncertain']):
        proposal += '\nIssue: Technical generation failure; repair and review required.\n'
    receipt = dict(retry_path=str(path.resolve()), source_sha256=meta['source_sha256'],
                   attempt_sha256=hitl.sha(hitl.encoded(selected)), proposal_sha256=hitl.sha(proposal.encode()))
    if destination.exists():
        if load_json((destination / 'retry-origin.json').read_text(encoding='utf-8')) != receipt:
            raise ValueError('conflicting fallback session')
        return hitl.status(destination)
    hitl.create(destination, original)
    hitl.write(destination / 'proposal.txt', proposal.encode())
    hitl.write(destination / 'retry-origin.json', hitl.encoded(receipt))
    # Archive every prompt, raw response and diagnostic with the human draft.
    hitl.write(destination / 'retry-events.jsonl', (path / 'events.jsonl').read_bytes())
    return hitl.validate(destination)


def run(path, source, *, max_retries=2, total_seconds=45.0, base_url='http://localhost:11434',
        client=None, admission=None, clock=time.monotonic):
    if type(max_retries) is not int or not 0 <= max_retries <= 2:
        raise ValueError('max_retries must be 0, 1 or 2')
    if isinstance(total_seconds, bool) or not isinstance(total_seconds, (int, float)) or not math.isfinite(total_seconds) or not 0 < total_seconds <= 45:
        raise ValueError('automatic budget must be in (0, 45] seconds')
    source_check(source)
    config = dict(policy=POLICY, source_sha256=hitl.sha(source.encode()), max_retries=max_retries,
                  total_seconds=total_seconds, min_call_seconds=MIN_CALL_SECONDS,
                  decision_reserve=DECISION_RESERVE, model=agent.MODEL, options=agent.OPTIONS)
    path = Path(path)
    with campaign_lock():
        if path.exists():
            if load_json((path / 'config.json').read_text(encoding='utf-8')) != config or (path / 'original.txt').read_bytes() != source.encode():
                raise ValueError('retry configuration/source mismatch')
        else:
            path.mkdir(parents=True)
            hitl.write(path / 'config.json', hitl.encoded(config))
            hitl.write(path / 'original.txt', source.encode())
        saved = attempts(path)
        rows = journal(path)
        if rows and rows[-1]['kind'] == 'finish':
            return rows[-1]['result']
        elapsed = saved[-1]['automatic_seconds'] if saved else 0.0
        anchor = clock()
        wait_budget = WaitBudget()
        observations = []
        stop = None
        if saved:
            last = saved[-1]
            if not last['diagnosis']['eligible']:
                stop = last['diagnosis']['status'], last['diagnosis']['reason']
            elif len(saved) > 1 and last['signature'] == saved[-2]['signature']:
                stop = 'ABSTAIN', 'repeated_without_progress'
        while stop is None and len(saved) < max_retries + 1:
            remaining = total_seconds - elapsed - (clock() - anchor)
            if remaining < MIN_CALL_SECONDS + DECISION_RESERVE:
                stop = 'TIME_BUDGET_EXHAUSTED', 'insufficient_time_for_call'
                break
            # Initial admission is initialization, measured separately. Subsequent
            # admission and waits consume the same automatic deadline.
            check_start = clock()
            admitted = admission(remaining) if admission else gate(agent.MODEL, base_url,
                wait_budget=wait_budget, deadline=time.monotonic() + remaining - DECISION_RESERVE)
            check_seconds = clock() - check_start
            append(path, kind='admission', index=len(saved), seconds=check_seconds, result=admitted)
            if not saved:
                anchor = clock()
            if not admitted['allowed']:
                stop = 'ABSTAIN', 'resource_refused'
                break
            remaining = total_seconds - elapsed - (clock() - anchor) - DECISION_RESERVE
            if remaining < MIN_CALL_SECONDS:
                stop = 'TIME_BUDGET_EXHAUSTED', 'insufficient_time_for_call'
                break
            payload = request(source, saved[-1] if saved else None)
            index = len(saved)
            append(path, kind='start', index=index, request=payload, timeout_seconds=remaining)
            started = clock()
            row = dict(index=index, request=payload, raw=None, response=None, error=None,
                       termination_uncertain=True, signature=None)
            try:
                response = client(payload, remaining) if client else agent.ollama_chat(payload, base_url, remaining, observations)
                row['response'] = response
                row['termination_uncertain'] = response.get('done') is not True
                row['raw'] = response['message']['content']
                if not isinstance(row['raw'], str):
                    raise ValueError('response content must be text')
                row['generation_seconds'] = clock() - started
                validation_start = clock()
                row['diagnosis'] = diagnose(source, row['raw'])
                row['signature'] = signature(row['raw']) if row['diagnosis']['validation']['syntax_valid'] else None
                row['validation_seconds'] = clock() - validation_start
                if row['termination_uncertain'] or response.get('done_reason') == 'length':
                    row['error'] = 'uncertain_termination' if row['termination_uncertain'] else 'truncated'
                    row['diagnosis'].update(status='ABSTAIN', eligible=False, reason=row['error'], technical=row['error'])
            except Exception as error:
                row['error'] = f'{type(error).__name__}: {error}'
                if not isinstance(row['raw'], str):
                    row['raw'] = None
                row['generation_seconds'] = clock() - started
                row['validation_seconds'] = 0.0
                row['diagnosis'] = dict(status='TIME_BUDGET_EXHAUSTED' if isinstance(error, TimeoutError) else 'ABSTAIN',
                    reason='timeout' if isinstance(error, TimeoutError) else 'transport_or_contract_error',
                    eligible=False, technical=type(error).__name__, validation=hitl.parse(''))
            row['automatic_seconds'] = elapsed + clock() - anchor
            append(path, kind='attempt', attempt=row)
            saved.append(row)
            if row['automatic_seconds'] >= total_seconds:
                stop = 'TIME_BUDGET_EXHAUSTED', 'automatic_deadline'
            elif not row['diagnosis']['eligible']:
                stop = row['diagnosis']['status'], row['diagnosis']['reason']
            elif len(saved) > 1 and row['signature'] == saved[-2]['signature']:
                stop = 'ABSTAIN', 'repeated_without_progress'
        if stop is None:
            stop = 'CALL_BUDGET_EXHAUSTED', 'max_retries_reached'
        result = dict(status=stop[0], reason=stop[1], calls=len(saved), max_retries=max_retries,
            automatic_seconds=elapsed + clock() - anchor, solver=None, verification=None,
            optimality_proven=False, observations=observations,
            termination_uncertain=bool(saved and saved[-1]['termination_uncertain']))
        if result['status'] == 'READY_VERIFIED':
            remaining = total_seconds - result['automatic_seconds'] - DECISION_RESERVE
            if remaining <= 0:
                result.update(status='TIME_BUDGET_EXHAUSTED', reason='no_solver_time')
            else:
                problem = Gap.model_validate(saved[-1]['diagnosis']['validation']['problem'])
                result['solver'] = solve(problem, min(5.0, remaining))
                candidate = result['solver']['candidate']
                result['verification'] = verify(problem, candidate) if candidate else None
                result['optimality_proven'] = bool(result['solver']['solver_proof'] and result['verification'] and result['verification']['valid'])
        result['automatic_seconds'] = elapsed + clock() - anchor
        if result['automatic_seconds'] >= total_seconds:
            result.update(status='TIME_BUDGET_EXHAUSTED', reason='automatic_deadline', optimality_proven=False)
        append(path, kind='finish', result=result)
        return result


def prefix(path, max_retries):
    """Evaluate saved sequential prefixes without any model or solver call."""
    if type(max_retries) is not int or not 0 <= max_retries <= 2:
        raise ValueError('max_retries must be 0, 1 or 2')
    rows = attempts(path)[:max_retries + 1]
    events = journal(path)
    final = events[-1]['result'] if events and events[-1]['kind'] == 'finish' else None
    if not rows:
        return dict(attempts=[], status=final['status'] if final else 'ABSTAIN', reason=final['reason'] if final else 'no_attempt',
                    automatic_seconds=final['automatic_seconds'] if final else 0, solver=None, verification=None)
    last = rows[-1]
    status, reason = last['diagnosis']['status'], last['diagnosis']['reason']
    if last['diagnosis']['eligible']:
        status, reason = 'CALL_BUDGET_EXHAUSTED', 'max_retries_reached'
        if len(rows) > 1 and last['signature'] == rows[-2]['signature']:
            status, reason = 'ABSTAIN', 'repeated_without_progress'
    result = dict(attempts=rows, status=status, reason=reason, automatic_seconds=last['automatic_seconds'], solver=None, verification=None)
    if final and len(rows) == len(attempts(path)):
        result.update({key: final[key] for key in ('status', 'reason', 'automatic_seconds', 'solver', 'verification')})
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest='operation', required=True)
    command = operations.add_parser('run')
    command.add_argument('path', type=Path)
    command.add_argument('--source', type=Path, required=True)
    command.add_argument('--max-retries', type=int, choices=(0, 1, 2), default=2)
    command.add_argument('--total-seconds', type=float, default=45)
    command.add_argument('--base-url', default='http://localhost:11434')
    command = operations.add_parser('status')
    command.add_argument('path', type=Path)
    command.add_argument('--max-retries', type=int, choices=(0, 1, 2), default=2)
    command = operations.add_parser('fallback')
    command.add_argument('path', type=Path)
    command.add_argument('session', type=Path)
    command.add_argument('--index', type=int, default=-1)
    args = parser.parse_args()
    if args.operation == 'run':
        result = run(args.path, args.source.read_text(encoding='utf-8-sig'), max_retries=args.max_retries,
                     total_seconds=args.total_seconds, base_url=args.base_url)
    elif args.operation == 'fallback':
        result = fallback(args.path, args.session, args.index)
    else:
        result = prefix(args.path, args.max_retries)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
