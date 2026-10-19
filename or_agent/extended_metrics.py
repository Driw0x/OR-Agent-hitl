"""Offline metrics for the extended corpus; historical scores remain unchanged."""

from collections import Counter
import itertools
import math
import statistics

from .agent import explanation_valid, preserved
from .evaluate import assess as historical_assess, canonical
from .models import Extraction, Gap, load_json
from .verify import verify


def _same_unit(case, field, actual, expected):
    if actual == expected:
        return True
    for group in case.get('unit_equivalences', {}).get(field, []):
        if isinstance(group, (list, tuple)) and actual in group and expected in group:
            return True
    return None


def _fidelity(case, extraction):
    empty = dict(numerical=False, semantic=False, strict=False, units=None)
    if case['disposition'] != 'ready' or not isinstance(extraction, dict) or extraction.get('disposition') != 'ready':
        return empty
    try:
        actual = canonical(extraction['problem'])
        expected = canonical(case['problem'])
    except (ValueError, TypeError, KeyError):
        return empty
    numerical = all(actual[key] == expected[key] for key in ('tasks', 'resources', 'capacities', 'pairs'))
    units = [_same_unit(case, key, actual[key], expected[key]) for key in ('cost_unit', 'resource_unit')]
    units = True if all(value is True for value in units) else None
    return dict(numerical=numerical, units=units,
                semantic=units if numerical else False, strict=actual == expected)


def _raw(case, attempt):
    result = dict(json=False, schema=False, provenance=False, coverage=None,
                  field_coverage={}, extraction=None)
    raw = attempt.get('raw')
    if not isinstance(raw, str):
        result.update(_fidelity(case, None))
        return result
    try:
        parsed = load_json(raw)
        result['json'] = True
        result['extraction'] = parsed
        result.update(_fidelity(case, parsed))
        extraction = Extraction.model_validate(parsed)
        result['schema'] = True
        extraction.check_source(case['source'])
        result['provenance'] = True
        if extraction.disposition == 'ready':
            fields = {}
            for field in Gap.model_fields:
                evidence = case.get('field_evidence', {}).get(field)
                fields[field] = (all(quote in extraction.provenance[field] for quote in evidence)
                                 if evidence else None)
            result['field_coverage'] = fields
            result['coverage'] = (False if False in fields.values() else
                                  True if all(value is True for value in fields.values()) else None)
    except (ValueError, TypeError, KeyError, AttributeError):
        pass
    if 'numerical' not in result:
        result.update(_fidelity(case, result['extraction']))
    return result


def _failure_stage(result, last, accepted):
    explicit = result.get('failure_stage')
    if explicit in ('load', 'transport', 'generation', 'resource', 'mathematical', 'semantic'):
        return explicit
    if result['outcome'] == 'resource_blocked':
        return 'resource'
    if result['outcome'] == 'execution_error':
        error = ' '.join(str(a.get('error') or '') for a in result['attempts']).lower()
        if any(value in error for value in ('urlerror', 'connectionerror', 'connectionrefused', 'timeouterror', 'http error')):
            return 'transport'
        return 'execution_unknown'
    if 'budget' in result.get('reason', '') and (not result['attempts'] or not result['attempts'][-1].get('raw')):
        return 'budget'
    if result.get('reason') in ('correction_changes_locked_data', 'unknown_extraction_fields_cannot_be_removed'):
        return 'correction_policy'
    if not last['json'] or not last['schema']:
        return 'generation'
    if not last['provenance']:
        return 'provenance'
    if accepted['semantic'] is not True:
        return 'semantic'
    if result['outcome'] in ('verified', 'infeasible'):
        return 'mathematical'
    if result['outcome'] == 'abstain':
        return 'solver_or_budget'
    return None


def assess(case, result):
    """Replay raw text, schema, quotations and reference arithmetic independently.

    Unit aliases count only when the corpus explicitly lists their equivalence.
    Coverage means coverage of annotated evidence, not general language entailment.
    Feasibility ignores the reported objective; objective accuracy is separate.
    """
    historical = historical_assess(case, result)
    attempts = result['attempts']
    first = _raw(case, attempts[0] if attempts else {})
    last = _raw(case, attempts[-1] if attempts else {})
    accepted = _fidelity(case, result.get('extraction'))
    accepted_valid = False
    try:
        accepted_extraction = Extraction.model_validate(result.get('extraction'))
        accepted_extraction.check_source(case['source'])
        accepted_valid = True
    except (ValueError, TypeError):
        pass
    ready = case['disposition'] == 'ready'
    feasible = ready and case['reference_optimum'] is not None
    check = (verify(Gap.model_validate(case['problem']), result['candidate'])
             if ready and result.get('candidate') else None)
    solution_feasible = bool(check and not any(v['kind'] != 'objective' for v in check['violations']))
    objective_correct = bool(check and check['valid'])
    reference_optimal = bool(solution_feasible and feasible and
                             check['recomputed_objective'] == case['reference_optimum'])
    guarantees_valid = bool(explanation_valid(result) and not historical['false_validity_claim']
                            and not historical['false_optimality_claim']
                            and (result['outcome'] != 'infeasible'
                                 or result.get('solver', {}).get('raw_status') == 'INFEASIBLE'))
    if ready:
        if feasible:
            correct = bool(result['outcome'] == 'verified' and accepted['semantic'] is True
                           and objective_correct and reference_optimal and guarantees_valid)
        else:
            correct = bool(result['outcome'] == 'infeasible' and accepted['semantic'] is True
                           and result.get('solver', {}).get('raw_status') == 'INFEASIBLE'
                           and guarantees_valid)
    else:
        correct = result['outcome'] == case['disposition'] and guarantees_valid
    correct = bool(correct and accepted_valid)
    corrected = len(attempts) > 1
    first_problem = first['extraction'].get('problem') if isinstance(first['extraction'], dict) else None
    last_problem = last['extraction'].get('problem') if isinstance(last['extraction'], dict) else None
    preservation_eligible = corrected and isinstance(first_problem, dict)
    data_preserved = preserved(first_problem, last_problem) if preservation_eligible else None
    regression_eligible = bool(corrected and first['schema'] and first['provenance'] and first['semantic'] is True)
    data_regression_eligible = bool(corrected and first['numerical'])
    stage = None if correct else _failure_stage(result, last, accepted)
    if not correct and not ready and stage == 'semantic':
        stage = 'disposition'
    # Keep the frozen calculation intact and visible even where the new scope differs.
    return dict(historical=historical,
                json_first=first['json'], json_last=last['json'],
                schema_first=first['schema'], schema_last=last['schema'],
                provenance_first=first['provenance'], provenance_last=last['provenance'],
                provenance_semantic_coverage=last['coverage'], provenance_field_coverage=last['field_coverage'],
                numerical_fidelity_first=first['numerical'], numerical_fidelity=last['numerical'],
                numerical_fidelity_accepted=accepted['numerical'],
                unit_semantics=last['units'], semantic_fidelity=last['semantic'],
                semantic_fidelity_accepted=accepted['semantic'],
                strict_fidelity_raw=last['strict'], strict_fidelity_accepted=accepted['strict'],
                accepted_extraction_valid=accepted_valid,
                final_answer_correct=correct, reference_solution_feasible=solution_feasible,
                reference_objective_correct=objective_correct, reference_optimal=reference_optimal,
                correct_infeasible=bool(ready and not feasible and correct),
                clarification_appropriate=bool(case['disposition'] == 'clarify' and correct),
                disposition_correct=result['outcome'] == case['disposition'] if not ready else result['outcome'] in ('verified', 'infeasible'),
                abstention=result['outcome'] == 'abstain',
                appropriate_abstention=bool(not ready and correct),
                false_guarantee=not guarantees_valid,
                false_validity_claim=historical['false_validity_claim'],
                false_optimality_claim=historical['false_optimality_claim'],
                reference_answer_mismatch=bool(result['outcome'] in ('verified', 'infeasible') and not correct),
                correction_attempted=corrected, correction_success=bool(corrected and correct),
                correction_failure=bool(corrected and not correct),
                correction_preservation_eligible=preservation_eligible,
                correction_data_preserved=data_preserved,
                correction_policy_violation=bool(preservation_eligible and not data_preserved),
                correction_regression_eligible=regression_eligible,
                correction_regression=bool(regression_eligible and not (last['schema'] and last['provenance']
                                                                            and last['semantic'] is True and result.get('extraction'))),
                correction_data_regression_eligible=data_regression_eligible,
                correction_data_regression=bool(data_regression_eligible and not last['numerical']),
                outcome=result['outcome'], failure_stage=stage,
                **{key: historical[key] for key in ('input_tokens', 'output_tokens', 'load_seconds',
                    'generation_seconds', 'solver_seconds', 'elapsed_seconds', 'llm_calls', 'tool_calls')})


def wilson(count, denominator):
    """Descriptive 95% Wilson interval; no random-sample claim for this corpus."""
    if not denominator:
        return None
    z = 1.959963984540054
    proportion = count / denominator
    scale = 1 + z * z / denominator
    centre = (proportion + z * z / (2 * denominator)) / scale
    half = z * math.sqrt(proportion * (1 - proportion) / denominator + z * z / (4 * denominator**2)) / scale
    return [max(0.0, centre - half), min(1.0, centre + half)]


def exact_paired_p(left_only, right_only):
    """Two-sided exact McNemar test, equivalent to a discordant-pair binomial test."""
    discordant = left_only + right_only
    return min(1.0, 2 * sum(math.comb(discordant, k) for k in range(min(left_only, right_only) + 1)) / 2**discordant)


_RATES = {
    'json_first': 'all', 'json_last': 'all', 'schema_first': 'all', 'schema_last': 'all',
    'provenance_first': 'ready', 'provenance_last': 'ready', 'provenance_semantic_coverage': 'ready',
    'numerical_fidelity_first': 'ready', 'numerical_fidelity': 'ready',
    'numerical_fidelity_accepted': 'ready', 'unit_semantics': 'ready', 'semantic_fidelity': 'ready',
    'semantic_fidelity_accepted': 'ready', 'strict_fidelity_raw': 'ready', 'strict_fidelity_accepted': 'ready',
    'final_answer_correct': 'all', 'reference_solution_feasible': 'feasible',
    'reference_objective_correct': 'feasible', 'reference_optimal': 'feasible',
    'correct_infeasible': 'infeasible', 'clarification_appropriate': 'clarify',
    'disposition_correct': 'all', 'abstention': 'all', 'appropriate_abstention': 'nonready',
    'false_guarantee': 'all', 'false_validity_claim': 'all', 'false_optimality_claim': 'all',
    'reference_answer_mismatch': 'all', 'correction_attempted': 'all',
    'correction_success': 'correction_attempted', 'correction_failure': 'correction_attempted',
    'correction_data_preserved': 'correction_preservation_eligible',
    'correction_policy_violation': 'correction_preservation_eligible',
    'correction_regression': 'correction_regression_eligible',
    'correction_data_regression': 'correction_data_regression_eligible',
}


def _eligible(case, name):
    ready = case['disposition'] == 'ready'
    return {'all': True, 'ready': ready, 'feasible': ready and case['reference_optimum'] is not None,
            'infeasible': ready and case['reference_optimum'] is None,
            'clarify': case['disposition'] == 'clarify', 'nonready': not ready}.get(name)


def _aggregate(planned, rows, cases, intervals=False):
    by_index = {row['index']: row['metrics'] for row in rows}
    rates = {}
    for key, scope in _RATES.items():
        slots = [item for item in planned if (_eligible(cases[item['case_id']], scope)
                 if not scope.startswith('correction_') else by_index.get(item['index'], {}).get(scope, False))]
        values = [by_index[item['index']].get(key) for item in slots if item['index'] in by_index]
        count = sum(value is True for value in values)
        rates[key] = dict(count=count, denominator=len(slots), observed=len(values),
                          unknown=sum(value is None for value in values), missing=len(slots) - len(values))
        if intervals and key == 'final_answer_correct':
            rates[key]['wilson_95_descriptive'] = wilson(count, len(slots))
    summary = dict(planned=len(planned), completed=len(rows), missing=len(planned) - len(rows), **rates)
    summary['outcomes'] = dict(Counter(row['metrics']['outcome'] for row in rows))
    summary['failure_stages'] = dict(Counter(row['metrics']['failure_stage'] for row in rows if row['metrics']['failure_stage']))
    for key in ('elapsed_seconds', 'input_tokens', 'output_tokens', 'load_seconds', 'generation_seconds',
                'solver_seconds', 'llm_calls', 'tool_calls'):
        values = [row['metrics'][key] for row in rows if row['metrics'][key] is not None]
        summary[key] = dict(sum=sum(values) if values else None,
                            mean=statistics.mean(values) if values else None,
                            median=statistics.median(values) if values else None,
                            min=min(values) if values else None, max=max(values) if values else None,
                            available=len(values), missing=len(rows) - len(values))
    return summary


def _paired(planned, rows, methods):
    expected = {(item['case_id'], item['method']) for item in planned}
    observed = {(row['case_id'], row['method']): row['metrics']['final_answer_correct'] for row in rows}
    result = {}
    for left, right in itertools.combinations(methods, 2):
        ids = sorted({case_id for case_id, method in expected if method == left and (case_id, right) in expected})
        complete = [case_id for case_id in ids if (case_id, left) in observed and (case_id, right) in observed]
        left_only = [case_id for case_id in complete if observed[case_id, left] and not observed[case_id, right]]
        right_only = [case_id for case_id in complete if observed[case_id, right] and not observed[case_id, left]]
        result[f'{left}/{right}'] = dict(planned_pairs=len(ids), observed_pairs=len(complete),
            missing_pairs=[case_id for case_id in ids if case_id not in complete],
            left_only=left_only, right_only=right_only,
            both_correct=sum(observed[case_id, left] and observed[case_id, right] for case_id in complete),
            both_incorrect=sum(not observed[case_id, left] and not observed[case_id, right] for case_id in complete),
            exact_mcnemar_p=exact_paired_p(len(left_only), len(right_only)),
            test_scope='complete pairs in the primary repetition; exploratory, unadjusted for multiple comparisons')
    return result


def summarize(header, records, cases):
    """Recompute all metrics. Keep missing runs; never pool repeats for inference."""
    if isinstance(cases, dict) and 'cases' in cases:
        cases = cases['cases']
    if not isinstance(cases, dict):
        if len({case['id'] for case in cases}) != len(cases):
            raise ValueError('duplicate reference case ID')
        cases = {case['id']: case for case in cases}
    planned = [dict(item, index=index, repetition=item.get('repetition', 1))
               for index, item in enumerate(header['planned'])]
    identities = [(item['case_id'], item['method'], item['repetition']) for item in planned]
    if len(set(identities)) != len(identities):
        raise ValueError('duplicate planned case/method/repetition')
    if any(item['case_id'] not in cases or item['method'] not in header['methods']
           or type(item['repetition']) is not int or item['repetition'] < 1 for item in planned):
        raise ValueError('unknown planned case, method or repetition')
    seen = set()
    rows = []
    for record in records:
        if record.get('kind', 'result') != 'result':
            continue
        index = record['index']
        if type(index) is not int or not 0 <= index < len(planned) or index in seen:
            raise ValueError('duplicate or unplanned result index')
        expected = planned[index]
        if any(record.get(key, 1 if key == 'repetition' else None) != expected[key]
               for key in ('case_id', 'method', 'repetition')):
            raise ValueError('result differs from planned identity')
        for key in ('model', 'model_digest'):
            if key in expected and record.get(key) != expected[key]:
                raise ValueError('result differs from planned model identity')
        seen.add(index)
        rows.append(dict(record, metrics=assess(cases[record['case_id']], record['result'])))
    repetitions = sorted({item['repetition'] for item in planned})
    primary = header.get('primary_repetition', 1)
    if repetitions and primary not in repetitions:
        raise ValueError('primary repetition absent from plan')
    primary_plan = [item for item in planned if item['repetition'] == primary]
    primary_rows = [row for row in rows if planned[row['index']]['repetition'] == primary]
    def group(items, observed, intervals=False):
        return {method: _aggregate([item for item in items if item['method'] == method],
                                  [row for row in observed if row['method'] == method], cases, intervals)
                for method in header['methods']}
    categories = sorted({cases[item['case_id']].get('category', 'unspecified') for item in primary_plan})
    return dict(primary_repetition=primary, methods=group(primary_plan, primary_rows, True),
                protocol_planned=len(planned), protocol_completed=len(rows),
                repetition_status={str(repetition): ('complete' if sum(planned[row['index']]['repetition'] == repetition for row in rows)
                    == sum(item['repetition'] == repetition for item in planned) else
                    'planned_conditionally_not_started' if repetition != primary and header.get('repetitions_condition')
                    and not any(planned[row['index']]['repetition'] == repetition for row in rows) else 'incomplete')
                    for repetition in repetitions},
                by_category={category: group([item for item in primary_plan if cases[item['case_id']].get('category', 'unspecified') == category],
                    [row for row in primary_rows if cases[row['case_id']].get('category', 'unspecified') == category]) for category in categories},
                paired=_paired(primary_plan, primary_rows, header['methods']),
                repetitions={str(repetition): group([item for item in planned if item['repetition'] == repetition],
                    [row for row in rows if planned[row['index']]['repetition'] == repetition]) for repetition in repetitions},
                all_observations_descriptive=group(planned, rows),
                scope='Fixed synthetic corpus; Wilson intervals descriptive only. Missing slots are not successes. '
                      'Exact paired tests use only primary-repetition complete pairs; repeated observations are not independent.',
                provenance_scope='Syntax checks exact source quotations. Semantic coverage checks corpus-annotated evidence only.',
                guarantee_scope='False guarantees concern claims about the extracted model; reference mismatches remain separate.')
