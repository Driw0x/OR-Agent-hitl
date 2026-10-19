"""Opt-in, source-only GAP facts. Unrecognized text never proves completeness."""

from collections import defaultdict
from copy import deepcopy
import hashlib
import json
import re
import time
from typing import Literal

from . import agent
from .crosscheck import gap_facts
from .experimental import STRUCTURED_DIRECTIVE, structured_gap
from .models import ClosedModel, Extraction, Gap, Identifier, Unit, load_json

ID = r'[A-Za-z][A-Za-z0-9_-]{0,31}'
INTEGER = r'-?(?:0|[1-9][0-9]*)'
UNIT = r'[A-Za-z][A-Za-z0-9_-]{0,79}'
GAP_SENTENCES = ('every listed task is mandatory and indivisible. '
    'Assign each task to exactly one listed resource. Minimize the sum of assignment costs. '
    'Each resource limit applies cumulatively to its assigned tasks. There are no other constraints.')
HYBRID_INSTRUCTION = """Deterministic facts below supplement the unchanged source.
Preserve verified identifiers, values and exact unit tokens as locked constraints.
Spans are zero-based Python character offsets [start,end) into the original source.
Tuple columns are field,resource,task,value,unit,start,end; null task means capacity.
Only verified facts are locked. Unknown/partial facts are not established values.
Interpret remaining text; never invent missing data. Clarify essential unknowns.
Do not silently repair contradictions. Return the same GAP extraction schema.
"""


class SourceFact(ClosedModel):
    field: Literal['costs', 'consumption', 'capacities']
    resource: Identifier
    task: Identifier | None
    value: int
    unit: Unit | None
    evidence: str
    start: int
    end: int
    verification: Literal['verified', 'unknown', 'contradiction']


def extract(source, *, observe=None):
    """Recognize labelled rows, explicit records and declarations, never references.

    Partial structured rows require a declared unit. Complete reconstruction additionally
    requires a closed structured grammar or a fully consumed record grammar.
    """
    if not isinstance(source, str) or not source.strip() or len(source) > 8000:
        raise ValueError('source must contain1..8000 characters')
    if observe:
        observe('extraction', 'running')
    declarations = defaultdict(list)
    facts, uninterpreted, constraints = [], [], []
    rows = []
    offset = 0
    for raw_line in source.splitlines(keepends=True):
        line = raw_line.rstrip('\r\n')
        rows.append((line, offset, offset + len(line)))
        offset += len(raw_line)
    used = set()

    def declaration(field, value, line, start, end):
        declarations[field].append(dict(value=value, evidence=line, start=start, end=end))

    for line, start, end in rows:
        match = re.fullmatch(r'(Tasks|Resources): (' + ID + r'(?:, ?' + ID + r')*)\.', line)
        if match:
            declaration(match[1].lower(), re.split(r', ?', match[2]), line, start, end)
            used.add(start)
        match = re.fullmatch(r'Cost unit: (' + UNIT + r')\. Resource unit: (' + UNIT + r')\.', line)
        if match:
            for field, value in zip(('cost_unit', 'resource_unit'), match.groups()):
                declaration(field, value, line, start, end)
            used.add(start)
        for field, label in (('cost_unit', 'Cost unit'), ('resource_unit', 'Resource unit')):
            match = re.fullmatch(label + r': (' + UNIT + r')\.', line)
            if match:
                declaration(field, match[1], line, start, end)
                used.add(start)
        if line == STRUCTURED_DIRECTIVE or re.fullmatch(
                r'At the [A-Za-z][A-Za-z -]* workshop, ' + re.escape(GAP_SENTENCES), line):
            constraints.append(dict(value='standard_gap_only', evidence=line, start=start, end=end))
            used.add(start)

    established, declaration_conflicts = {}, []
    for field, entries in declarations.items():
        values = [entry['value'] for entry in entries]
        if all(value == values[0] for value in values) and (
                not isinstance(values[0], list) or len(set(values[0])) == len(values[0])):
            established[field] = values[0]
        else:
            declaration_conflicts.append(field)

    def add(field, resource, task, value, unit, line, start, end):
        facts.append(SourceFact(field=field, resource=resource, task=task, value=value, unit=unit,
            evidence=line, start=start, end=end,
            verification='verified' if unit is not None else 'unknown').model_dump())

    for line, start, end in rows:
        match = re.fullmatch(r'Assignment record: T=(' + ID + r'); R=(' + ID + r'); cost=('
            + INTEGER + r') (' + UNIT + r'); consumption=(' + INTEGER + r') (' + UNIT + r')\.', line)
        if match:
            task, resource, cost, cost_unit, amount, resource_unit = match.groups()
            add('costs', resource, task, int(cost), cost_unit, line, start, end)
            add('consumption', resource, task, int(amount), resource_unit, line, start, end)
            used.add(start)
            continue
        match = re.fullmatch(r'Cumulative capacity of (' + ID + r'): (' + INTEGER + r') (' + UNIT + r')\.', line)
        if match:
            add('capacities', match[1], None, int(match[2]), match[3], line, start, end)
            used.add(start)
            continue
        match = re.fullmatch(r'(' + ID + r') (costs|consumption): (.+)\.', line)
        if match:
            resource, field, body = match.groups()
            cells = body.split(', ')
            parsed = [re.fullmatch(r'(' + ID + r')=(' + INTEGER + r')', cell) for cell in cells]
            if all(parsed):
                for cell in parsed:
                    add(field, resource, cell[1], int(cell[2]), established.get(
                        'cost_unit' if field == 'costs' else 'resource_unit'), line, start, end)
                used.add(start)
        match = re.fullmatch(r'(' + ID + r') capacity: (' + INTEGER + r')\.', line)
        if match:
            add('capacities', match[1], None, int(match[2]), established.get('resource_unit'), line, start, end)
            used.add(start)

    if observe:
        observe('extraction', 'completed')
        observe('data', 'running')
    grouped = defaultdict(list)
    issues = [dict(kind='conflicting_declaration', field=field) for field in declaration_conflicts]
    for fact in facts:
        assert source[fact['start']:fact['end']] == fact['evidence']
        grouped[(fact['field'], fact['resource'], fact['task'])].append(fact)
        field = 'cost_unit' if fact['field'] == 'costs' else 'resource_unit'
        if field in established and fact['unit'] is not None and fact['unit'] != established[field]:
            fact['verification'] = 'contradiction'
            issues.append(dict(kind='unit_mismatch', field=fact['field'], resource=fact['resource'], task=fact['task']))
        for field, identifier in (('resources', fact['resource']), ('tasks', fact['task'])):
            if identifier is not None and field in established and identifier not in established[field]:
                issues.append(dict(kind='unlisted_identifier', field=field, identifier=identifier))
        bound = 10**9
        if abs(fact['value']) > bound or (fact['field'] != 'costs' and fact['value'] < 0):
            issues.append(dict(kind='outside_gap_domain', field=fact['field'], resource=fact['resource'], task=fact['task'], value=fact['value']))
    for key, entries in grouped.items():
        if len({(fact['value'], fact['unit']) for fact in entries}) > 1:
            for fact in entries:
                fact['verification'] = 'contradiction'
            issues.append(dict(kind='conflicting_values', field=key[0], resource=key[1], task=key[2]))
    verified = {key: entries[0] for key, entries in grouped.items()
                if all(fact['verification'] == 'verified' for fact in entries)}
    tasks, resources = established.get('tasks'), established.get('resources')
    unknown = []
    coverage = {}
    for field in ('costs', 'consumption', 'capacities'):
        expected = ([(field, r, None) for r in resources] if field == 'capacities' else
                    [(field, r, t) for r in resources for t in tasks]) if tasks and resources else None
        missing = [list(key) for key in expected if key not in verified] if expected is not None else None
        unknown.append(dict(field=field, missing=missing, reason='unestablished_association' if missing else
                            'inventory_unknown' if expected is None else None))
        coverage[field] = dict(expected=len(expected) if expected is not None else None,
                              verified=sum(key in verified for key in expected) if expected is not None else
                              sum(key[0] == field for key in verified))
    for field in ('tasks', 'resources', 'cost_unit', 'resource_unit'):
        if field not in established:
            unknown.append(dict(field=field, missing=None, reason='declaration_unknown'))
    if not constraints:
        unknown.append(dict(field='constraints', missing=None, reason='standard_gap_not_proven'))
    if observe:
        observe('data', 'completed', coverage=coverage, contradictions=issues)
        observe('constraints', 'running')
    for line, start, end in rows:
        if start not in used:
            uninterpreted.append(dict(evidence=line, start=start, end=end))
    complete = None
    proof = None
    try:
        problem, evidence = structured_gap(source)
        complete, proof = problem.model_dump(), 'structured_gap_v1'
    except ValueError:
        if not issues and not uninterpreted and constraints and tasks and resources and all(
                row['missing'] == [] for row in unknown):
            data = {field: established[field] for field in ('tasks', 'resources', 'cost_unit', 'resource_unit')}
            data['capacities'] = [verified[('capacities', r, None)]['value'] for r in resources]
            for field in ('costs', 'consumption'):
                data[field] = [[verified[(field, r, t)]['value'] for t in tasks] for r in resources]
            complete, proof = Gap.model_validate(data).model_dump(), 'closed_labelled_records_v1'
    if observe:
        observe('constraints', 'completed', recognized=constraints, uninterpreted=uninterpreted)
    return dict(version='deterministic_gap_v1', source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        declarations=dict(declarations), established=established, facts=facts, unknown=unknown,
        contradictions=issues, uninterpreted=uninterpreted, constraints=constraints, coverage=coverage,
        complete_problem=complete, complete_proof=proof)


def context(report):
    """Compact facts retain exact source spans; no repeated source quotations."""
    return dict(verified=[[f[k] for k in ('field','resource','task','value','unit','start','end')]
        for f in report['facts'] if f['verification'] == 'verified'],
        declarations=report['established'],
        partial=[f for f in report['facts'] if f['verification'] == 'unknown'],
        unknown=[row for row in report['unknown'] if row['reason']],
        contradictions=report['contradictions'],
        uninterpreted_spans=[[row['start'], row['end']] for row in report['uninterpreted']],
        standard_gap_proven=bool(report['constraints']))


def request(source, mode='source_only'):
    if mode not in ('source_only', 'locked_facts'):
        raise ValueError('LLM mode must be source_only or locked_facts')
    payload = dict(model=agent.MODEL, messages=[dict(role='system', content=agent.INSTRUCTION),
        dict(role='user', content=source)], stream=False, options=agent.OPTIONS.copy(),
        keep_alive='5m', format=Extraction.model_json_schema())
    if mode == 'locked_facts':
        payload['messages'][0]['content'] += '\n' + HYBRID_INSTRUCTION + json.dumps(context(extract(source)), separators=(',', ':'))
    return payload


def check_locks(source, problem):
    report = extract(source)
    actual = gap_facts(problem)
    differences = []
    for field, value in report['established'].items():
        other = problem[field]
        changed = set(value) != set(other) if isinstance(value, list) else value != other
        if changed:
            differences.append(dict(field=field, expected=value, actual=other))
    for fact in report['facts']:
        if fact['verification'] != 'verified':
            continue
        key = (fact['field'], fact['resource'], fact['task'])
        if actual.get(key) != (fact['value'], fact['unit']):
            differences.append(dict(field=key[0], resource=key[1], task=key[2],
                expected=[fact['value'], fact['unit']], actual=actual.get(key), evidence=fact['evidence']))
    return dict(accepted=not differences and not report['contradictions'], differences=differences,
                source_contradictions=report['contradictions'], full_source_verified=report['complete_problem'] is not None)


def run(source, mode='locked_facts', *, client=None, admission=None, base_url='http://localhost:11434', wait_budget=None):
    """Use the single-call structured controller; intercept locked changes before solving."""
    if mode not in ('source_only', 'locked_facts', 'deterministic'):
        raise ValueError('mode must be source_only, locked_facts or deterministic')
    started = time.monotonic()
    report = extract(source)
    captured = {}
    if mode == 'deterministic' and report['complete_problem'] is None:
        result = agent.run(source, 'structured', max_calls=0)
        result.update(reason='deterministic_incomplete', method='deterministic', llm_calls=0, deterministic=report)
        result['answer'] = agent.answer_for(result)
        return result

    def controlled(payload, timeout):
        if mode == 'deterministic':
            extraction = dict(disposition='ready', problem=report['complete_problem'], issues=[],
                provenance={field: source for field in Gap.model_fields})
            return dict(message=dict(content=json.dumps(extraction)), deterministic=True)
        expected = request(source, mode)
        if mode == 'source_only':
            assert payload == expected
        captured['request'] = deepcopy(expected)
        response = client(expected, timeout) if client else agent.ollama_chat(expected, base_url, timeout, captured.setdefault('observations', []))
        captured['response'] = response
        try:
            parsed = load_json(response['message']['content'])
            if isinstance(parsed, dict) and parsed.get('problem') is not None:
                problem = Gap.model_validate(parsed['problem']).model_dump()
                captured['locks'] = check_locks(source, problem)
                if mode == 'locked_facts' and not captured['locks']['accepted']:
                    raise LockedFactError('locked_fact_mismatch')
        except (ValueError, TypeError, KeyError):
            # Schema/provenance failures are handled by the unchanged controller.
            pass
        return response

    result = agent.run(source, 'structured', client=controlled, admission=(lambda: dict(allowed=True, reason='no_llm'))
        if mode == 'deterministic' else admission, base_url=base_url, wait_budget=wait_budget)
    if mode != 'deterministic' and result['attempts']:
        attempt = result['attempts'][0]
        if 'request' in captured:
            attempt['request'] = captured['request']
        if result['reason'].endswith('locked_fact_mismatch'):
            response = captured['response']
            attempt.update(response=response, raw=response['message']['content'], json_valid=True)
            try:
                Extraction.model_validate(load_json(attempt['raw']))
                attempt['structural_valid'] = True
            except ValueError:
                pass
            result.update(outcome='abstain', reason='locked_fact_mismatch')
    result.update(method=mode, deterministic=report, lock_check=captured.get('locks'),
                  llm_calls=0 if mode == 'deterministic' else len(result['attempts']), elapsed_seconds=time.monotonic()-started)
    result['observations'].extend(captured.get('observations', []))
    if mode == 'deterministic':
        result['attempts'][0]['origin'] = 'deterministic_reconstruction_no_llm'
    result['answer'] = agent.answer_for(result)
    result['explanation_valid'] = agent.explanation_valid(result)
    return result


class LockedFactError(RuntimeError):
    """Controller catches this before any CP-SAT invocation."""
