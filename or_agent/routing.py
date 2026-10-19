"""Opt-in deterministic admission certificate and existing HITL sessions. GAP only."""

from collections import defaultdict
import math
import re
from pathlib import Path
import time

from . import deterministic as d, hitl
from .models import Gap, load_json

VERSION = 's2_hitl_v1'
GRAMMAR = 'closed_labelled_gap_v1'


def source_scope(source, report):
    """Conservative known exclusions; absence of a match is NOT a prose proof.

    Only the opt-in human scope policy uses this screen. Unknown formulations
    remain the reviewer's responsibility; detected exclusions cannot be waived.
    """
    pattern = (r'pr[ée]c[ée]dence|pr[ée]c[éèe]d(?:e|er)|precedence|precede|avant\b|before\b|apr[èe]s\b|after\b|'
        r'deadline|[ée]ch[ée]ance|ordonnancement|scheduling|routing|routage|tourn[ée]e|'
        r'exclusion|incompatib|simultan|interdit|forbidden|at most one task|au plus une t[âa]che|'
        r'au moins|at least|must.*first|doit.*premier|use .* first|maximi[sz]|'
        r'other constraint applies|autre contrainte.*(?:impos|oblig)|unknown condition|unknown constraint|'
        r'must use|must be assigned to|must be on|cannot (?:perform|use|execute)|'
        r'doit (?:utiliser|[êe]tre affect[ée])|ne peut pas.*(?:effectu|ex[ée]cut|affect)|'
        r'vehicle route|visiting every customer|voyageur de commerce|'
        r'if .* (?:uses|assigned)|si .* (?:utilise|affect|alors)|'
        r'omit(?:ted)?|optional task|t[âa]che.*facultativ|twice|deux fois|both resources|'
        r'same resource|different resources|m[êe]me ressource|ressources diff[ée]rentes|'
        r'interpret.*(?:cost|consum)|interpr[ée]t.*(?:co[ûu]t|consomm)|'
        r'only .* (?:may execute|allowed|permitted)|au plus [0-9]+ t[âa]ches')
    exclusions = [dict(evidence=source[m.start():m.end()], start=m.start(), end=m.end())
                  for m in re.finditer(pattern, source, flags=re.IGNORECASE)]
    return dict(unsupported=exclusions, contradictions=report['contradictions'],
        notice='Known exclusions only, not a universal language classifier. Explicit human GAP scope confirmation is required.')


def certify(source, report=None):
    """Certify the closed grammar, never arbitrary prose or user intent.

    Reconstruct from labelled facts rather than using matrix dimensions as
    association evidence. An optional report is checked, never trusted.
    """
    fresh = d.extract(source)
    if report is not None and report != fresh:
        raise ValueError('deterministic report does not match unchanged source')
    report = fresh
    inventory = report['established']
    tasks, resources = inventory.get('tasks', []), inventory.get('resources', [])
    groups = defaultdict(list)
    for fact in report['facts']:
        groups[(fact['field'], fact['resource'], fact['task'])].append(fact)
    fields_complete = bool(tasks and resources) and all(
        row['expected'] is not None and row['verified'] == row['expected'] for row in report['coverage'].values())
    units_checked = all(inventory.get(k) for k in ('cost_unit', 'resource_unit')) and all(
        f['unit'] == inventory.get('cost_unit' if f['field'] == 'costs' else 'resource_unit') for f in report['facts'])
    associations_checked = bool(groups) and all(
        f['verification'] == 'verified' and f['resource'] in resources and
        (f['task'] is None if f['field'] == 'capacities' else f['task'] in tasks) and
        source[f['start']:f['end']] == f['evidence'] for f in report['facts'])
    reconstructed = None
    if fields_complete and associations_checked and units_checked and not report['contradictions']:
        data = {k: inventory[k] for k in ('tasks', 'resources', 'cost_unit', 'resource_unit')}
        data['capacities'] = [groups[('capacities', r, None)][0]['value'] for r in resources]
        for field in ('costs', 'consumption'):
            data[field] = [[groups[(field, r, t)][0]['value'] for t in tasks] for r in resources]
        reconstructed = Gap.model_validate(data).model_dump()
    checks = dict(numerical_fields_complete=fields_complete,
        associations_verified=associations_checked, units_checked=bool(units_checked),
        supported_constraints_represented=bool(report['constraints']),
        relevant_source_consumed=not report['uninterpreted'], contradictions_absent=not report['contradictions'],
        declarations_complete=all(k in inventory for k in ('tasks', 'resources', 'cost_unit', 'resource_unit')),
        canonical_reconstruction_matches=reconstructed is not None and reconstructed == report['complete_problem'],
        closed_grammar_recognized=report['complete_proof'] in ('structured_gap_v1', 'closed_labelled_records_v1'))
    blockers = [key for key, passed in checks.items() if not passed]
    if not blockers:
        status = 'VERIFIED_COMPLETE'
    elif report['contradictions']:
        status = 'INVALID'
    elif not report['facts']:
        status = 'UNRECOGNIZED'
    elif report['uninterpreted']:
        status = 'UNSUPPORTED'  # Grammar unsupported; no universal prose classifier.
    else:
        status = 'PARTIAL'
    result = dict(version=VERSION, grammar=GRAMMAR, parser=report['version'],
        admission_sha256=hitl.sha(Path(__file__).read_bytes()),
        canonical_contract_sha256=hitl.sha(Path(d.__file__).with_name('models.py').read_bytes()),
        parser_sha256=hitl.sha(Path(d.__file__).read_bytes()),
        p1_parser_sha256=hitl.sha(Path(d.__file__).with_name('experimental.py').read_bytes()),
        source_sha256=hitl.sha(source.encode('utf-8')), status=status, checks=checks, blockers=blockers,
        inventories={key: inventory.get(key) for key in ('resources', 'tasks')},
        units={key: inventory.get(key) for key in ('cost_unit', 'resource_unit')},
        numerical_coverage=report['coverage'], constraints=report['constraints'], facts=report['facts'],
        declarations=report['declarations'], unknown=report['unknown'],
        uninterpreted=report['uninterpreted'], contradictions=report['contradictions'],
        canonical_sha256=hitl.sha(hitl.encoded(reconstructed)) if not blockers else None,
        scope='Closed labelled GAP grammar only; exact unit tokens, no conversion or general prose guarantee.')
    result['certificate_sha256'] = hitl.sha(hitl.encoded(result))
    return result


def manual_proposal(report):
    """Copy only individually verified facts; every absent datum remains ?."""
    established = report['established']
    tasks, resources = established.get('tasks', []), established.get('resources', [])
    cost_unit, resource_unit = (established.get(k, '?') for k in ('cost_unit', 'resource_unit'))
    lines = [hitl.HEADER, hitl.OBJECTIVE, hitl.CONSTRAINTS,
        'Tasks: ' + (', '.join(tasks) or '?') + '.',
        'Resources: ' + (', '.join(resources) or '?') + '.',
        f'Cost unit: {cost_unit}.', f'Resource unit: {resource_unit}.']
    facts = {(f['field'], f['resource'], f['task']): f for f in report['facts'] if f['verification'] == 'verified'}
    def number(field, resource, task=None):
        fact = facts.get((field, resource, task))
        return str(fact['value']) if fact else '?'
    for r in resources:
        lines.append(f'Cumulative capacity of {r}: {number("capacities", r)} {resource_unit}.')
        for t in tasks:
            lines.append(f'Assignment record: T={t}; R={r}; cost={number("costs", r, t)} {cost_unit}; consumption={number("consumption", r, t)} {resource_unit}.')
    if report['contradictions']:
        lines.append('Issue: Original source contradicts itself. Submit an explicitly corrected request in a new session.')
    for passage in report['uninterpreted']:
        if passage['evidence'].strip():
            lines.append('Issue: Source outside recognized grammar: ' + passage['evidence'])
    if not report['constraints']:
        lines.append('Issue: Original source GAP constraint scope is unverified. Submit a clarified request.')
    return '\n'.join(lines) + '\n'


def guard(path, source, validation):
    """Persistent source blockers cannot be removed by editing a proposal.

    A corrected source requires a new session. Keeping original bytes immutable
    also prevents reuse of an approval for a changed request.
    """
    path = Path(path)
    saved = load_json((path / 'routing.json').read_text(encoding='utf-8'))
    certificate = certify(source.decode('utf-8-sig'))
    if saved['version'] != VERSION or saved['branch'] != 'HITL' or saved['certificate'] != certificate:
        raise ValueError('routing certificate fingerprint mismatch')
    blockers = []
    if certificate['contradictions']:
        blockers.append('Original source contradiction requires an explicitly corrected request in a new session.')
    human_scope = saved.get('scope_policy') == 'human_gap_v1'
    if human_scope:
        scope = source_scope(source.decode('utf-8-sig'), d.extract(source.decode('utf-8-sig')))
        validation['scope_confirmation_required'] = True
        validation['scope_screen'] = scope
        if scope['unsupported']:
            blockers.append('Original source contains a detected unsupported constraint; submit a corrected request in a new session.')
    elif any(row['evidence'].strip() for row in certificate['uninterpreted']) or not certificate['constraints']:
        blockers.append('Source scope unverified: unsupported constraints cannot be approved away. Submit a corrected closed-grammar request in a new session.')
    validation['source_blockers'] = blockers
    validation['source_certificate'] = certificate['certificate_sha256']
    validation['fidelity_notice'] = ('Copied partial facts are proved only within recognized lines. Compare all original data and constraints. '
        'The explicitly approved formulation is authoritative; implicit intentions remain unverified.')
    if blockers:
        validation['complete'] = False
        validation['errors'] += blockers
    return validation


def submit(path, source, *, llm=False, base_url='http://localhost:11434', client=None,
           admission=None, seconds=5.0, total_seconds=180.0, human_scope=False, observe=None):
    """Analyze once, solve certified GAP or preserve a single editable HITL draft."""
    if isinstance(seconds, bool) or not math.isfinite(seconds) or not 0 <= seconds <= 3600:
        raise ValueError('solver seconds must be finite and between 0 and 3600')
    if not isinstance(source, bytes):
        raise ValueError('source must be UTF-8 bytes')
    text = source.decode('utf-8-sig')
    started = time.perf_counter()
    report = d.extract(text, observe=observe)
    parsed = time.perf_counter()
    if observe:
        observe('certificate', 'running')
    certificate = certify(text, report)
    if observe:
        observe('certificate', 'completed', certificate_status=certificate['status'])
        observe('routing', 'running')
    certified = time.perf_counter()
    branch = 'AUTONOMOUS' if certificate['status'] == 'VERIFIED_COMPLETE' else 'HITL'
    decision = dict(version=VERSION, branch=branch, original_sha256=hitl.sha(source), certificate=certificate,
        timings=dict(parse_seconds=parsed-started, admission_seconds=certified-parsed), llm_requested=bool(llm and branch == 'HITL'))
    if human_scope:
        decision['scope_policy'] = 'human_gap_v1'
    path = Path(path)
    # Establish the persistent routing marker before generation. HITL remains
    # responsible for all review, approval and reconstruction authority.
    hitl.create(path, source)
    session, _ = hitl.load(path)
    session.routing_version = VERSION
    hitl.event(path, session, 'routed', branch=branch, certificate_sha256=certificate['certificate_sha256'])
    hitl.write(path / 'routing.json', hitl.encoded(decision))
    hitl.write(path / 's2.json', hitl.encoded(report))
    if observe:
        observe('routing', 'completed', branch=branch)
    decision['timings']['session_setup_seconds'] = time.perf_counter() - certified
    if branch == 'AUTONOMOUS':
        hitl.write(path / 'canonical.json', hitl.encoded(report['complete_problem']))
        if observe:
            for stage in ('generation', 'review', 'scope', 'approval'):
                observe(stage, 'not_applicable')
        solve(path, seconds, observe=observe)
    else:
        hitl.write(path / 'proposal.txt', manual_proposal(report).encode('utf-8'))
        if llm:
            hitl.propose(path, source, base_url=base_url, client=client, admission=admission,
                         total_seconds=total_seconds, existing=True)
        else:
            hitl.validate(path)
    decision['timings']['total_automatic_seconds'] = time.perf_counter() - started
    hitl.write(path / 'routing.json', hitl.encoded(decision))
    return status(path)


def inspect(path):
    path = Path(path)
    session, original = hitl.load(path)
    if session.routing_version != VERSION:
        raise ValueError('session is not an experimental routing session')
    saved = load_json((path / 'routing.json').read_text(encoding='utf-8'))
    text = original.decode('utf-8-sig')
    certificate = certify(text)
    if saved['version'] != VERSION or saved['original_sha256'] != hitl.sha(original) or saved['certificate'] != certificate:
        raise ValueError('routing certificate fingerprint mismatch')
    report = d.extract(text)
    if load_json((path / 's2.json').read_text(encoding='utf-8')) != report:
        raise ValueError('saved source evidence mismatch')
    expected = 'AUTONOMOUS' if certificate['status'] == 'VERIFIED_COMPLETE' else 'HITL'
    if saved['branch'] != expected:
        raise ValueError('routing branch mismatch')
    if expected == 'AUTONOMOUS' and (path / 'canonical.json').read_bytes() != hitl.encoded(report['complete_problem']):
        raise ValueError('canonical fingerprint mismatch')
    return saved, report


def status(path):
    """Read saved results; never generate or solve during consultation."""
    path = Path(path)
    decision, report = inspect(path)
    result = load_json((path / 'result.json').read_text(encoding='utf-8')) if (path / 'result.json').exists() else None
    if result and decision['branch'] == 'AUTONOMOUS' and result.get('certificate_sha256') != decision['certificate']['certificate_sha256']:
        raise ValueError('result certificate mismatch')
    if result and decision['branch'] == 'AUTONOMOUS':
        session, _ = hitl.load(path)
        if not any(event.get('action') == 'autonomous_solved' and event.get('result_sha256') == hitl.sha(hitl.encoded(result)) for event in session.events):
            raise ValueError('saved result fingerprint mismatch')
    output = dict(routing=decision, deterministic=report, result=result)
    if decision['branch'] == 'HITL':
        output['hitl'] = hitl.status(path)
        if result:
            output['result_current'] = bool(output['hitl']['approval_valid'] and
                result.get('fingerprints') == output['hitl']['fingerprints'])
    return output


def solve(path, seconds=5.0, *, observe=None):
    if isinstance(seconds, bool) or not math.isfinite(seconds) or not 0 <= seconds <= 3600:
        raise ValueError('solver seconds must be finite and between 0 and 3600')
    path = Path(path)
    decision, report = inspect(path)
    if decision['branch'] == 'HITL':
        current = hitl.status(path)
        if not current['approval_valid']:
            raise ValueError('current explicit approval required before solving')
        if (path / 'result.json').exists():
            previous = load_json((path / 'result.json').read_text(encoding='utf-8'))
            if previous.get('fingerprints') == current['fingerprints']:
                return previous
        return hitl.solve(path, seconds, observe=observe)
    if (path / 'result.json').exists():
        return status(path)['result']
    certificate = decision['certificate']
    result = dict(branch='AUTONOMOUS', certificate_sha256=certificate['certificate_sha256'],
        source_sha256=decision['original_sha256'], canonical_sha256=certificate['canonical_sha256'],
        problem=report['complete_problem'], llm_calls=0,
        **hitl.solve_problem(Gap.model_validate(report['complete_problem']), seconds, observe=observe),
        guarantee='Problème reconstruit selon la grammaire reconnue et résolu sans intervention du LLM.')
    # Revalidate after solving as well; changing session evidence never creates
    # a current result for a different source or canonical model.
    inspect(path)
    hitl.write(path / 'result.json', hitl.encoded(result))
    session, _ = hitl.load(path)
    session.state = 'SOLVED'
    hitl.event(path, session, 'autonomous_solved', result_sha256=hitl.sha(hitl.encoded(result)))
    return result
