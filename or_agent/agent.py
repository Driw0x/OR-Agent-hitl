"""Bounded orchestration; the LLM emits data only, never executable code."""

from datetime import datetime, timezone
import hashlib
import json
import math
import queue
import threading
import time
from urllib.request import Request, urlopen

from .models import Extraction, load_json, read_extraction
from .resources import WaitBudget, gate, snapshot
from .solver import solve
from .verify import verify

MODEL = "qwen2.5-coder:7b"
OPTIONS = dict(temperature=0, seed=17, num_ctx=4096, num_predict=1024, num_thread=4)
INSTRUCTION = """Extract only a Generalized Assignment Problem. Never execute code.
Return one JSON object, no markdown. Matrices use resource rows and task columns.
Use only explicit source data, strict integers, no invented defaults or rounding.
Exactly one resource per task, cumulative capacity constraints, minimize total cost.
Unknown/missing/ambiguous data: clarify. Extra constraints (deadlines, routing,
scheduling): unsupported. Contradictory data: invalid. Never ignore a constraint.
Object keys: disposition (ready/clarify/unsupported/invalid), problem, issues, provenance.
If ready: issues=[], problem has tasks, resources, costs, consumption, capacities,
cost_unit, resource_unit. Every problem field needs provenance: map each of those
seven field names to an exact nonempty quote from source. Quotes need not be distinct.
Otherwise: problem=null, issues=[specific reason or question], provenance={}.
Identifiers: letter followed by letters/digits/_/-, max32. Unique task/resource IDs.
Costs: integers -1000000000..1000000000; consumption/capacities: integers0..1000000000.
Both units must be stated. Never infer unstated units or values.
"""


def ollama_chat(payload, base_url, timeout, observations):
    """Bound client wait; monitor own request without killing a shared server."""
    completed = queue.Queue()
    def request():
        try:
            req = Request(base_url.rstrip('/') + '/api/chat',
                          data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})
            with urlopen(req, timeout=max(0.01, timeout)) as response:
                body = response.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024:
                raise ValueError('Ollama response exceeds 4MiB')
            completed.put((True, load_json(body.decode())))
        except Exception as error:
            completed.put((False, error))
    threading.Thread(target=request, daemon=True).start()
    deadline = time.monotonic() + timeout
    next_monitor = time.monotonic() + 10
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('client deadline; shared Ollama cancellation is not guaranteed')
        try:
            ok, value = completed.get(timeout=min(1, remaining))
            if not ok:
                raise value
            return value
        except queue.Empty:
            pass
        if time.monotonic() >= next_monitor:
            current = snapshot(base_url, timeout=min(0.5, max(0.01,(deadline-time.monotonic()-0.1)/2)))
            observations.append(dict(tool='resource_monitor', result=current))
            if any(current.get(k) is not None and current[k] < 1024**3
                   for k in ('ram_available_bytes','commit_available_bytes')):
                raise MemoryError('emergency memory reserve below1GiB; client stops, shared server untouched')
            next_monitor = time.monotonic() + 10


def preserved(previous, current):
    """Every parseable data leaf is immutable, including unknown constraints."""
    if isinstance(previous, dict):
        return isinstance(current, dict) and all(k in current and preserved(v, current[k]) for k,v in previous.items())
    if isinstance(previous, list):
        return isinstance(current, list) and len(current) >= len(previous) and all(preserved(v,current[i]) for i,v in enumerate(previous))
    return type(previous) is type(current) and previous == current


def answer_for(result):
    outcome = result['outcome']
    fidelity = ('Source fidelity verified only for structured_gap_v1; no general language guarantee.'
                if result.get('fidelity') == 'verified' and
                result.get('evidence', {}).get('scope') == 'structured_gap_v1_only' else
                'Source fidelity is unassessed; inspect extraction.')
    if outcome == 'verified':
        proof = 'Exact optimum proven for extracted model.' if result['optimality_proven'] else 'Feasible for extracted model; optimality unproven.'
        return f"{proof} Cost {result['candidate']['objective']}. {fidelity}"
    if outcome == 'infeasible':
        if result.get('fidelity') == 'verified':
            return f'CP-SAT proves the extracted model infeasible. {fidelity}'
        return 'CP-SAT proves the extracted model infeasible. Source fidelity is unassessed.'
    return f"No verified assignment. Outcome: {outcome}. {result.get('reason','')}"


def explanation_valid(result):
    return (result.get('answer') == answer_for(result)
            and (not result.get('optimality_proven') or
                 (result.get('verification',{}).get('valid') is True and result.get('solver',{}).get('solver_proof') is True))
            and (result.get('candidate') is None or result.get('verification',{}).get('valid') is True))


def run(source, method='structured-repair', model=MODEL, base_url='http://localhost:11434', *,
        max_calls=None, total_seconds=180.0, solver_seconds=5.0, client=None,
        admission=None, wait_budget=None, provenance='single_excerpt', diagnostics='validation_error', fidelity_policy=None):
    if provenance not in ('single_excerpt', 'multiple_excerpts') or diagnostics not in ('validation_error', 'structured_diagnostics'):
        raise ValueError('experimental variants must be single_excerpt/multiple_excerpts and validation_error/structured_diagnostics')
    if fidelity_policy not in (None, 'contradictions', 'verified'):
        raise ValueError('fidelity_policy must be None, contradictions or verified')
    contract, reader = Extraction, read_extraction
    instruction = INSTRUCTION
    if provenance == 'multiple_excerpts':
        from .experimental import MultiExtraction, read_multi
        contract, reader = MultiExtraction, read_multi
        instruction = INSTRUCTION.replace(
            'seven field names to an exact nonempty quote from source. Quotes need not be distinct.',
            'seven field names to a nonempty list of exact nonblank source excerpts. '
            'Keep separate excerpts separate; do not join or paraphrase them. '
            'Include all data rows, identifiers and separate unit declarations for the field.')
    if method not in ('json','structured','structured-repair'):
        raise ValueError('method must be json, structured or structured-repair')
    calls = (2 if method == 'structured-repair' else 1) if max_calls is None else max_calls
    if type(calls) is not int or not 0 <= calls <= (2 if method == 'structured-repair' else 1):
        raise ValueError('LLM call budget exceeds generation strategy contract')
    if not all(math.isfinite(x) and x >= 0 for x in (total_seconds,solver_seconds)):
        raise ValueError('time budgets must be finite and nonnegative')
    if not source.strip() or len(source) > 8000:
        raise ValueError('source must contain1..8000 characters')
    started = time.monotonic()
    result = dict(outcome='abstain',reason='call_budget_exhausted',candidate=None,
                  optimality_proven=False,fidelity='unassessed',source=source,
                  source_sha256=hashlib.sha256(source.encode()).hexdigest(),method=method,model=model,
                  timestamp_utc=datetime.now(timezone.utc).isoformat(),options=OPTIONS.copy(),
                  budgets=dict(max_calls=calls,total_seconds=total_seconds,solver_seconds=solver_seconds),
                  attempts=[],observations=[],extraction=None)
    if provenance != 'single_excerpt' or diagnostics != 'validation_error':
        result['experimental'] = dict(provenance=provenance, diagnostics=diagnostics)
    if fidelity_policy is not None:
        result.setdefault('experimental', dict(provenance=provenance, diagnostics=diagnostics))['fidelity_policy'] = fidelity_policy
    messages = [dict(role='system',content=instruction),dict(role='user',content=source)]
    locks = None
    budget = wait_budget if wait_budget is not None else WaitBudget()
    for attempt_number in range(calls):
        remaining = total_seconds - (time.monotonic()-started)
        if remaining <= 0:
            result['reason']='elapsed_budget_exhausted'
            break
        original_limit = budget.operation_seconds
        budget.operation_seconds = min(original_limit, remaining)
        try:
            resource = admission() if admission is not None else gate(model,base_url,wait_budget=budget,deadline=started+total_seconds)
        finally:
            budget.operation_seconds = original_limit
        result['observations'].append(dict(tool='resource_gate',result=resource))
        if not resource['allowed']:
            result.update(outcome='resource_blocked',reason=resource['reason'])
            break
        remaining = total_seconds - (time.monotonic()-started)
        if remaining <= 0:
            result['reason']='elapsed_budget_exhausted'
            break
        payload = dict(model=model,messages=list(messages),stream=False,options=OPTIONS.copy(),keep_alive='5m')
        if method != 'json':
            payload['format'] = contract.model_json_schema()
        record = dict(number=attempt_number+1,request=payload,raw=None,response=None,error=None,
                      json_valid=False,structural_valid=False)
        result['attempts'].append(record)
        call_started = time.monotonic()
        try:
            response = client(payload,remaining) if client is not None else ollama_chat(payload,base_url,remaining,result['observations'])
            record['response'] = response
            raw = response['message']['content']
            record['raw'] = raw
        except Exception as error:
            record['error'] = f'{type(error).__name__}: {error}'
            result.update(outcome='execution_error',reason=record['error'])
            break
        finally:
            record['client_seconds'] = time.monotonic()-call_started
        if time.monotonic()-started >= total_seconds:
            result['reason']='elapsed_budget_exhausted'
            break
        parsed = None
        try:
            parsed = load_json(raw)
            record['json_valid'] = True
            if isinstance(parsed,dict) and set(parsed) - set(Extraction.model_fields):
                result['reason']='unknown_extraction_fields_cannot_be_removed'
                record['error']=result['reason']
                break
            data = parsed.get('problem') if isinstance(parsed,dict) else None
            if locks is not None and not preserved(locks,data):
                result['reason']='correction_changes_locked_data'
                record['error']=result['reason']
                break
            if isinstance(data,dict):
                locks = data
            extraction = reader(raw,source)
            record['structural_valid'] = True
            result['extraction'] = extraction.model_dump()
            result['observations'].append(dict(tool='validate',valid=True,disposition=extraction.disposition))
        except (ValueError,TypeError,AttributeError) as error:
            record['error']=str(error)
            result['observations'].append(dict(tool='validate',valid=False,error=str(error)))
            diagnostic = str(error)
            if diagnostics == 'structured_diagnostics':
                from .experimental import diagnose, diagnostic_feedback
                record['diagnostics'] = diagnose(raw,source,provenance,locked=locks is not None)
                diagnostic = json.dumps(diagnostic_feedback(record['diagnostics']),ensure_ascii=False)
            if method != 'structured-repair' or attempt_number+1 >= calls:
                result['reason']='invalid_extraction_call_budget_exhausted'
                break
            repair_instruction = (
                'Repair only diagnosed syntax/schema/provenance errors. Preserve source and every existing problem datum/constraint. '
                'Do not change problem values or remove problem fields. Add missing fields and correct provenance only when supported by the unchanged source. ')
            if locks is not None:
                repair_instruction += (
                    'The parsed problem is locked: problem=null and non-ready dispositions are forbidden. '
                    'If locked data prevent a valid repair, retain the extraction; the controller will abstain. ')
            else:
                repair_instruction += 'If a change is needed, ask clarification instead. '
            messages.extend([dict(role='assistant',content=raw),dict(role='user',content=repair_instruction+'Diagnostic: '+diagnostic)])
            continue
        if extraction.disposition != 'ready':
            result.update(outcome=extraction.disposition,reason='; '.join(extraction.issues))
            break
        if fidelity_policy is not None:
            from .experimental import check_fidelity
            result['fidelity_check'] = check_fidelity(extraction.problem, source)
            status = result['fidelity_check']['status']
            if status == 'mismatch' or (status == 'unassessed' and fidelity_policy == 'verified'):
                result['reason'] = 'fidelity_' + status
                break
        if provenance == 'multiple_excerpts':
            from .experimental import evidence_status
            result['evidence'] = evidence_status(extraction,source)
            if not result['evidence']['operationally_admissible']:
                result['reason'] = result['evidence'].get('reason', 'experimental_evidence_coverage_unassessed')
                break
            result['fidelity'] = result['evidence']['fidelity']
        if fidelity_policy is not None and result['fidelity_check']['status'] == 'verified':
            result['fidelity'] = 'verified'
            if provenance == 'single_excerpt':
                result['evidence'] = result['fidelity_check']
        remaining=total_seconds-(time.monotonic()-started)
        if remaining <= 0:
            result['reason']='elapsed_budget_exhausted'
            break
        solved=solve(extraction.problem,min(solver_seconds,remaining))
        result['solver']=solved
        result['observations'].append(dict(tool='solve',result=solved))
        if solved['candidate'] is not None:
            checked=verify(extraction.problem,solved['candidate'])
            result['verification']=checked
            result['observations'].append(dict(tool='verify',result=checked))
            if checked['valid']:
                result.update(outcome='verified',reason='independent_check_passed',candidate=solved['candidate'],
                              optimality_proven=solved['solver_proof'])
            else:
                result['reason']='checker_rejected'
        elif solved['raw_status']=='INFEASIBLE':
            result.update(outcome='infeasible',reason='solver_proof_for_extracted_model')
        else:
            result['reason']=solved.get('error') or solved['raw_status'] or 'execution_error'
        break
    result['elapsed_seconds']=time.monotonic()-started
    result['answer']=answer_for(result)
    result['explanation_valid']=explanation_valid(result)
    return result
