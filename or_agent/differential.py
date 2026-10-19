"""Opt-in differential proposals. Source-only locks; unconfirmed proposals never reach GAP.

The only extra automatic evidence rule is a closed ``Measurement: <existing
grammar line>`` envelope. Every line must be accounted for by the existing
parser before this envelope establishes facts. This is a syntactic scope, not
general prose entailment or evidence supplied by an LLM.
"""

from collections import Counter
import hashlib
import json
import re
import time
from typing import Literal

from pydantic import Field, model_validator

from . import agent, deterministic as d
from .models import ClosedModel, Gap, Identifier, Unit, load_json
from .resources import gate


class Proposal(ClosedModel):
    field: Literal['costs', 'consumption', 'capacities']
    resource: Identifier
    task: Identifier | None
    value: int = Field(ge=-10**9, le=10**9)
    unit: Unit
    evidence: str = Field(min_length=1, max_length=8000)
    uncertainty: Literal['certain', 'uncertain']
    clarification: str | None

    @model_validator(mode='after')
    def domain(self):
        if (self.field == 'capacities') != (self.task is None):
            raise ValueError('only capacity proposals have null task')
        if self.field != 'costs' and self.value < 0:
            raise ValueError('consumption and capacity must be nonnegative')
        if not self.evidence.strip() or not self.unit.strip():
            raise ValueError('evidence and unit must be nonblank')
        if self.uncertainty == 'uncertain' and not (self.clarification and self.clarification.strip()):
            raise ValueError('uncertain proposal requires clarification')
        return self


class Unresolved(ClosedModel):
    field: Literal['costs', 'consumption', 'capacities', 'tasks', 'resources', 'cost_unit', 'resource_unit', 'constraints']
    resource: Identifier | None
    task: Identifier | None
    evidence: str | None
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode='after')
    def nonblank(self):
        if not self.reason.strip():
            raise ValueError('clarification reason must be nonblank')
        return self


class Differential(ClosedModel):
    proposals: list[Proposal] = Field(max_length=10000)
    unresolved: list[Unresolved] = Field(max_length=10000)


INSTRUCTION = """Return only one differential JSON object: proposals and unresolved.
Never return a GAP, matrix, replacement inventory, or replacement constraint.
Locked facts and declarations are immutable. Propose only listed unknown cells.
Each proposal: field (costs/consumption/capacities), resource, task (null for
capacity), value (strict integer), exact unit, evidence (unchanged source quote),
uncertainty (certain/uncertain), clarification (null or specific question).
No default, invention, rounding or unit conversion. A quote is not semantic proof.
Unresolved entries: field, resource, task, evidence (or null), reason.
Missing/ambiguous units, associations or values require clarification. Mention
extra constraints and contradictions; never discard them. Unrecognized prose
can be proposed but requires external confirmation. Only a closed Measurement:
envelope around an existing grammar line is automatically checked by Python.
Your certainty never establishes verification. Return all essential unknowns.
"""


def request(source):
    """Accept source text only; benchmark labels and references have no input path."""
    report = d.extract(source)
    context = d.context(report)
    passages = report['uninterpreted'] + [dict(evidence=f['evidence'], start=f['start'], end=f['end'])
        for f in report['facts'] if f['verification'] != 'verified']
    # Known source rows are already represented once by compact locked tuples.
    view = dict(locked=context['verified'], declarations=context['declarations'],
        unknown=context['unknown'], contradictions=context['contradictions'],
        standard_gap_proven=context['standard_gap_proven'], passages=passages)
    return dict(model=agent.MODEL, messages=[dict(role='system', content=INSTRUCTION),
        dict(role='user', content=json.dumps(view, ensure_ascii=False, separators=(',', ':')))],
        stream=False, options=agent.OPTIONS.copy(), keep_alive='5m', format=Differential.model_json_schema())


def normalized(source):
    """One documented envelope, no generic substring/quotation entailment."""
    lines = [line[len('Measurement: '):] if line.startswith('Measurement: ') else line
             for line in source.splitlines()]
    text = '\n'.join(lines)
    try:
        report = d.extract(text)
    except ValueError:
        return text, None
    return text, report


def merge(source, delta):
    """Recompute locks from source; never accept a caller-supplied fact report."""
    delta = Differential.model_validate(load_json(delta) if isinstance(delta, str) else delta)
    report = d.extract(source)
    text, recognized = normalized(source)
    locked = {(f['field'], f['resource'], f['task']):(f['value'], f['unit'])
              for f in report['facts'] if f['verification']=='verified'}
    values = dict(locked)
    established = report['established']
    counts = Counter((p.field, p.resource, p.task) for p in delta.proposals)
    trusted = bool(recognized and not recognized['uninterpreted'] and not recognized['contradictions']
        and recognized['constraints'] and all(k in established for k in ('tasks','resources','cost_unit','resource_unit')))
    evidence_facts = recognized['facts'] if trusted else []
    accepted, rejected, unconfirmed = [], [], []
    for proposal in delta.proposals:
        p = proposal.model_dump()
        key = (proposal.field, proposal.resource, proposal.task)
        reason = None
        if counts[key] > 1:
            reason = 'duplicate_or_conflicting_proposals'
        elif key in locked:
            reason = 'locked_target'
        elif proposal.resource not in established.get('resources', []) or (
                proposal.task is not None and proposal.task not in established.get('tasks', [])):
            reason = 'unknown_identifier'
        elif proposal.evidence not in source:
            reason = 'inauthentic_quote'
        elif str(proposal.value) not in re.findall(r'(?<![\w.])-?\d+(?!\w|\.\d)', proposal.evidence):
            reason = 'value_absent_from_quote'
        unit = established.get('cost_unit' if proposal.field=='costs' else 'resource_unit')
        if not reason and unit is not None and proposal.unit != unit:
            reason = 'unit_mismatch'
        if reason:
            rejected.append(dict(proposal=p, reason=reason))
            continue
        matches = [f for f in evidence_facts if (f['field'],f['resource'],f['task'])==key
            and f['verification']=='verified' and f['evidence'] in proposal.evidence
            and ('Measurement: '+f['evidence']) in source.splitlines()]
        if matches and (proposal.value,proposal.unit) != (matches[0]['value'],matches[0]['unit']):
            rejected.append(dict(proposal=p, reason='value_or_association_mismatch'))
        elif proposal.uncertainty=='uncertain' or not matches or unit is None:
            unconfirmed.append(dict(proposal=p, reason='semantic_confirmation_required',
                source_sha256=report['source_sha256'], proposal_sha256=hashlib.sha256(
                    json.dumps(p,sort_keys=True).encode()).hexdigest()))
        else:
            values[key] = (proposal.value,proposal.unit)
            accepted.append(dict(proposal=p, proof='s3_measurement_envelope_v1'))
    assert all(values[k]==v for k,v in locked.items())
    missing = []
    for field in ('costs','consumption','capacities'):
        for r in established.get('resources', []):
            for t in ([None] if field=='capacities' else established.get('tasks', [])):
                if (field,r,t) not in values:
                    missing.append([field,r,t])
    outcome, problem, reason = 'clarify', None, 'essential_information_unconfirmed'
    if report['contradictions'] or (recognized and recognized['contradictions']):
        outcome, reason = 'invalid', 'source_contradiction'
    elif rejected:
        outcome, reason = 'abstain', 'rejected_proposals'
    elif not report['facts']:
        outcome, reason = 'abstain', 'no_useful_deterministic_facts'
    elif not trusted:
        outcome, reason = 'abstain', 'source_semantics_unproven'
    elif (not missing and not unconfirmed and not delta.unresolved and recognized['complete_problem']):
        p = {k:established[k] for k in ('tasks','resources','cost_unit','resource_unit')}
        p['capacities'] = [values[('capacities',r,None)][0] for r in p['resources']]
        for field in ('costs','consumption'):
            p[field] = [[values[(field,r,t)][0] for t in p['tasks']] for r in p['resources']]
        problem = Gap.model_validate(p).model_dump()
        assert d.check_locks(source, problem)['accepted']
        assert problem == recognized['complete_problem']
        outcome, reason = 'ready', 'all_required_facts_confirmed'
    return dict(outcome=outcome, reason=reason, problem=problem, accepted=accepted, rejected=rejected,
        unconfirmed=unconfirmed, unresolved=[x.model_dump() for x in delta.unresolved], missing=missing,
        locked_count=len(locked), locked_preserved=all(values[k]==v for k,v in locked.items()),
        source_sha256=report['source_sha256'], source_contradictions=report['contradictions'],
        normalized_s2_eligible=bool(recognized and recognized['complete_problem']),
        coverage=report['coverage'], normalized_source=text,
        guarantee='Only closed Measurement envelopes and existing grammar; prose proposals require user confirmation. No automatic unit conversion.')


def run(source, *, client=None, admission=None, base_url='http://localhost:11434', wait_budget=None):
    started = time.monotonic()
    report = d.extract(source)
    if report['complete_problem'] is not None:
        result = d.run(source, 'deterministic')
        result.update(method='differential_proposals', route='deterministic_direct', llm_calls=0)
        return result
    result = agent.run(source, 'structured', max_calls=0)
    result.update(method='differential_proposals', route='differential', deterministic=report, llm_calls=0)
    if not any(f['verification']=='verified' for f in report['facts']):
        result['reason'] = 'no_useful_deterministic_facts'
    else:
        admitted = admission() if admission else gate(agent.MODEL, base_url, wait_budget=wait_budget, deadline=started+180)
        result['observations'].append(dict(tool='resource_gate', result=admitted))
        if not admitted['allowed']:
            result.update(outcome='resource_blocked', reason=admitted['reason'])
        else:
            payload = request(source)
            attempt = dict(request=payload, raw=None, response=None, json_valid=False, structural_valid=False)
            result['attempts'].append(attempt)
            result['llm_calls'] = 1
            call_started = time.monotonic()
            try:
                remaining = 180-(time.monotonic()-started)
                if remaining <= 0:
                    raise TimeoutError('elapsed_budget_exhausted')
                response = client(payload, remaining) if client else agent.ollama_chat(payload, base_url, remaining, result['observations'])
                attempt.update(response=response, raw=response['message']['content'], client_seconds=time.monotonic()-call_started)
            except Exception as error:
                attempt['client_seconds'] = time.monotonic()-call_started
                result.update(outcome='execution_error', reason=f'{type(error).__name__}: {error}')
            else:
                try:
                    parsed = load_json(attempt['raw'])
                    attempt['json_valid'] = True
                    Differential.model_validate(parsed)
                    attempt['structural_valid'] = True
                    merged = merge(source, parsed)
                    result['merge'] = merged
                    if merged['outcome']=='ready' and time.monotonic()-started < 180:
                        extraction = dict(disposition='ready', problem=merged['problem'], issues=[],
                            provenance={k:source for k in Gap.model_fields})
                        solved = agent.run(source, 'structured', client=lambda *a:dict(message=dict(content=json.dumps(extraction))),
                            admission=lambda:dict(allowed=True, reason='verified_merge_no_llm'),
                            total_seconds=180-(time.monotonic()-started), solver_seconds=5)
                        for key in ('outcome','reason','extraction','solver','verification','candidate','optimality_proven'):
                            if key in solved:
                                result[key] = solved[key]
                    else:
                        result.update(outcome=merged['outcome'] if merged['outcome']!='ready' else 'abstain',
                            reason=merged['reason'] if merged['outcome']!='ready' else 'elapsed_budget_exhausted')
                        if result['outcome'] in ('clarify','invalid','unsupported'):
                            result['extraction'] = dict(disposition=result['outcome'], problem=None,
                                issues=[result['reason']], provenance={})
                except (ValueError, TypeError, KeyError) as error:
                    attempt['error'] = str(error)
                    result.update(outcome='abstain', reason='invalid_differential')
    result['elapsed_seconds'] = time.monotonic()-started
    result['answer'] = agent.answer_for(result)
    result['explanation_valid'] = agent.explanation_valid(result)
    return result
