"""Synthetic offline checks for extended scoring and paired denominators."""

from copy import deepcopy
import json

import pytest

from or_agent.agent import answer_for
from or_agent.evaluate import assess as historical_assess, enumerate_reference
from or_agent.extended_metrics import assess, exact_paired_p, summarize, wilson
from or_agent.models import Gap
from or_agent.verify import verify


def case(case_id='one', disposition='ready', impossible=False):
    problem = dict(tasks=['T1', 'T2'], resources=['R1', 'R2'], costs=[[1, 8], [4, 2]],
                   consumption=[[1, 1], [1, 1]], capacities=[0, 0] if impossible else [1, 1],
                   cost_unit='EUR', resource_unit='slots')
    evidence = {field: [f'{field}={json.dumps(value)}'] for field, value in problem.items()}
    source = '; '.join(quote for values in evidence.values() for quote in values)
    reference = enumerate_reference(problem)
    return dict(id=case_id, disposition=disposition, source=source, category='simple',
                problem=problem if disposition == 'ready' else None,
                reference_optimum=reference['optimum'] if disposition == 'ready' else None,
                reference_feasible_count=reference['feasible_count'] if disposition == 'ready' else None,
                field_evidence=evidence, unit_equivalences={})


def output(reference, problem=None):
    data = deepcopy(problem if problem is not None else reference['problem'])
    disposition = reference['disposition']
    extraction = dict(disposition=disposition, problem=data,
                      issues=[] if disposition == 'ready' else ['Please clarify missing data.'],
                      provenance={field: reference['source'] for field in data} if data else {})
    candidate = dict(assignments=[dict(task='T1', resource='R1'), dict(task='T2', resource='R2')],
                     objective=data['costs'][0][0] + data['costs'][1][1]) if data else None
    impossible = disposition == 'ready' and reference['reference_optimum'] is None
    if impossible:
        candidate = None
    result = dict(outcome='infeasible' if impossible else 'verified' if data else disposition,
                  candidate=candidate, optimality_proven=bool(candidate), reason='synthetic', extraction=extraction,
                  attempts=[dict(raw=json.dumps(extraction), json_valid=True, structural_valid=True,
                                 response=dict(prompt_eval_count=4, eval_count=3,
                                               load_duration=1_000_000, eval_duration=2_000_000))],
                  solver=dict(raw_status='INFEASIBLE' if impossible else 'OPTIMAL',
                              solver_proof=True, objective=candidate['objective'] if candidate else None,
                              best_bound=candidate['objective'] if candidate else None,
                              parameters=dict(absolute_gap_limit=0, relative_gap_limit=0), solver_wall_seconds=.001),
                  observations=[], elapsed_seconds=.1)
    if candidate:
        result['verification'] = verify(Gap.model_validate(data), candidate)
    result['answer'] = answer_for(result)
    return result


def failed(reference, stage=None):
    result = output(reference)
    result.update(outcome='execution_error', extraction=None, candidate=None, optimality_proven=False,
                  attempts=[dict(raw=None, json_valid=False, structural_valid=False, response=None,
                                 error='RuntimeError: synthetic failure')])
    if stage:
        result['failure_stage'] = stage
    result['answer'] = answer_for(result)
    return result


def plan(cases, methods=('json', 'structured', 'structured-repair'), repetitions=1):
    return dict(methods=list(methods), planned=[dict(case_id=reference['id'], method=method, repetition=repetition)
                for repetition in range(1, repetitions + 1) for reference in cases for method in methods])


def record(header, index, result):
    return dict(kind='result', index=index, **header['planned'][index], result=result)


def test_raw_schema_provenance_and_historical_scores_are_independent():
    reference = case()
    result = output(reference)
    metrics = assess(reference, result)
    assert metrics['historical'] == historical_assess(reference, result)
    assert all(metrics[key] for key in ('json_last', 'schema_last', 'provenance_last',
               'strict_fidelity_raw', 'strict_fidelity_accepted', 'provenance_semantic_coverage',
               'final_answer_correct', 'reference_solution_feasible', 'reference_optimal'))
    raw = json.loads(result['attempts'][0]['raw'])
    raw['provenance']['costs'] = 'not in source'
    result['attempts'][0]['raw'] = json.dumps(raw)
    metrics = assess(reference, result)
    assert metrics['json_last'] and metrics['schema_last'] and not metrics['provenance_last']
    assert metrics['strict_fidelity_raw']


@pytest.mark.parametrize('raw', ['{"x":1,"x":2}', 'NaN', '```json\n{}\n```', '{'])
def test_raw_json_replayed_without_trusting_stored_flags(raw):
    reference = case()
    result = output(reference)
    result['attempts'][0]['raw'] = raw
    metrics = assess(reference, result)
    assert not metrics['json_last'] and not metrics['schema_last']
    assert metrics['strict_fidelity_accepted']


@pytest.mark.parametrize('damage', ['array', 'missing_row', 'float', 'extra_field'])
def test_legal_json_does_not_imply_valid_schema_or_numerical_fidelity(damage):
    reference = case()
    result = output(reference)
    raw = json.loads(result['attempts'][0]['raw'])
    if damage == 'array':
        raw = [raw]
    elif damage == 'missing_row':
        raw['problem']['costs'].pop()
    elif damage == 'float':
        raw['problem']['costs'][0][0] = 1.0
    else:
        raw['problem']['deadline'] = 3
    result['attempts'][0]['raw'] = json.dumps(raw)
    metrics = assess(reference, result)
    assert metrics['json_last'] and not metrics['schema_last']
    assert not metrics['numerical_fidelity'] and not metrics['strict_fidelity_raw']


def test_only_explicit_unit_equivalences_can_establish_semantic_fidelity():
    reference = case()
    changed = deepcopy(reference['problem'])
    changed['cost_unit'] = 'euros'
    result = output(reference, changed)
    metrics = assess(reference, result)
    assert metrics['numerical_fidelity'] and not metrics['strict_fidelity_raw']
    assert metrics['semantic_fidelity'] is None and not metrics['final_answer_correct']
    reference['unit_equivalences'] = dict(cost_unit=[['EUR', 'euros']])
    metrics = assess(reference, result)
    assert metrics['semantic_fidelity'] is True and metrics['final_answer_correct']
    assert not metrics['historical']['answer_correct']


def test_reference_feasibility_and_optimality_do_not_require_faithful_extraction():
    reference = case()
    changed = deepcopy(reference['problem'])
    changed['costs'][0][1] += 10
    metrics = assess(reference, output(reference, changed))
    assert not metrics['numerical_fidelity'] and not metrics['final_answer_correct']
    assert metrics['reference_solution_feasible'] and metrics['reference_optimal']
    assert not metrics['false_guarantee']


def test_wrong_reported_objective_does_not_destroy_physical_feasibility():
    reference = case()
    result = output(reference)
    result['candidate']['objective'] += 1
    result['answer'] = answer_for(result)
    metrics = assess(reference, result)
    assert metrics['reference_solution_feasible'] and metrics['reference_optimal']
    assert not metrics['reference_objective_correct'] and not metrics['final_answer_correct']
    assert metrics['false_guarantee']


def test_feasible_suboptimal_answer_is_safe_but_not_correct_optimization_answer():
    reference = case()
    result = output(reference)
    result['candidate'] = dict(assignments=[dict(task='T1', resource='R2'), dict(task='T2', resource='R1')],
                               objective=12)
    result['optimality_proven'] = False
    result['verification'] = verify(Gap.model_validate(reference['problem']), result['candidate'])
    result['answer'] = answer_for(result)
    metrics = assess(reference, result)
    assert metrics['reference_solution_feasible'] and metrics['reference_objective_correct']
    assert not metrics['reference_optimal'] and not metrics['final_answer_correct']
    assert not metrics['false_guarantee'] and metrics['historical']['answer_correct']


def test_evidence_coverage_distinct_from_quote_syntax():
    reference = case()
    result = output(reference)
    raw = json.loads(result['attempts'][0]['raw'])
    raw['provenance']['costs'] = reference['field_evidence']['cost_unit'][0]
    result['attempts'][0]['raw'] = json.dumps(raw)
    metrics = assess(reference, result)
    assert metrics['provenance_last'] and metrics['provenance_semantic_coverage'] is False
    assert metrics['provenance_field_coverage']['costs'] is False
    reference.pop('field_evidence')
    assert assess(reference, result)['provenance_semantic_coverage'] is None


def test_infeasibility_and_clarification_denominators_use_references():
    cases = [case(), case('impossible', impossible=True), case('question', disposition='clarify')]
    header = plan(cases, methods=('structured',))
    rows = [record(header, index, output(reference)) for index, reference in enumerate(cases)]
    summary = summarize(header, rows, cases)['methods']['structured']
    assert summary['final_answer_correct']['count'] == 3
    assert summary['reference_optimal']['denominator'] == 1
    assert summary['correct_infeasible']['count'] == summary['correct_infeasible']['denominator'] == 1
    assert summary['clarification_appropriate']['count'] == summary['clarification_appropriate']['denominator'] == 1


def test_infeasible_outcome_without_solver_evidence_is_not_correct():
    reference = case(impossible=True)
    result = output(reference)
    result['solver']['raw_status'] = 'UNKNOWN'
    assert not assess(reference, result)['final_answer_correct']


def test_infeasibility_claim_without_solver_evidence_is_a_false_guarantee():
    reference = case(impossible=True)
    observed = {}
    for status in ('UNKNOWN', None):
        result = output(reference)
        if status is None:
            result.pop('solver')
        else:
            result['solver']['raw_status'] = status
        metrics = assess(reference, result)
        assert not metrics['final_answer_correct']
        observed['missing_solver' if status is None else status] = metrics['false_guarantee']
    assert all(observed.values()), observed


def test_present_quote_does_not_validate_an_incorrect_number():
    reference = case()
    changed = deepcopy(reference['problem'])
    changed['costs'][0][0] += 1
    metrics = assess(reference, output(reference, changed))
    assert metrics['provenance_last'] and metrics['accepted_extraction_valid']
    assert not metrics['numerical_fidelity'] and not metrics['semantic_fidelity']
    assert not metrics['strict_fidelity_raw'] and not metrics['final_answer_correct']


def test_incompatible_resource_units_require_an_explicit_conversion_contract():
    reference = case()
    changed = deepcopy(reference['problem'])
    changed['resource_unit'] = 'kilogram'
    metrics = assess(reference, output(reference, changed))
    assert reference['problem']['resource_unit'] == 'slots'
    assert metrics['numerical_fidelity'] and metrics['provenance_last']
    assert metrics['unit_semantics'] is None and metrics['semantic_fidelity'] is None
    assert not metrics['strict_fidelity_raw'] and not metrics['final_answer_correct']


def test_correction_regressions_need_admissible_initial_extraction():
    reference = case()
    result = output(reference)
    initial = deepcopy(result['attempts'][0])
    result['attempts'].insert(0, initial)
    assert assess(reference, result)['correction_success']
    damaged = json.loads(result['attempts'][1]['raw'])
    damaged['problem']['capacities'][0] = 0
    result['attempts'][1]['raw'] = json.dumps(damaged)
    result['extraction'] = damaged
    metrics = assess(reference, result)
    assert metrics['correction_regression_eligible'] and metrics['correction_regression']
    assert metrics['correction_failure'] and metrics['correction_data_regression']
    assert metrics['correction_preservation_eligible'] and metrics['correction_policy_violation']
    result['attempts'][0]['raw'] = 'bad JSON'
    metrics = assess(reference, result)
    assert not metrics['correction_regression_eligible'] and not metrics['correction_regression']


def test_correction_cannot_remove_unknown_constraints_even_from_invalid_initial_schema():
    reference = case()
    result = output(reference)
    initial = deepcopy(result['attempts'][0])
    parsed = json.loads(initial['raw'])
    parsed['problem']['deadline'] = 4
    initial['raw'] = json.dumps(parsed)
    result['attempts'].insert(0, initial)
    metrics = assess(reference, result)
    assert not metrics['schema_first'] and metrics['schema_last']
    assert metrics['correction_policy_violation'] and not metrics['correction_data_preserved']


@pytest.mark.parametrize('stage', ['load', 'transport', 'generation'])
def test_explicit_failure_stages_and_missing_tokens_survive_replay(stage):
    reference = case()
    metrics = assess(reference, failed(reference, stage))
    assert metrics['failure_stage'] == stage
    assert metrics['input_tokens'] is None and metrics['generation_seconds'] is None
    assert metrics['llm_calls'] == 1 and not metrics['final_answer_correct']


def test_missing_runs_keep_all_reference_denominators():
    references = [case(), case('missing', impossible=True)]
    header = plan(references, methods=('json',))
    summary = summarize(header, [record(header, 0, output(references[0]))], references)['methods']['json']
    assert (summary['planned'], summary['completed'], summary['missing']) == (2, 1, 1)
    assert summary['final_answer_correct']['count'] == 1 and summary['final_answer_correct']['denominator'] == 2
    assert summary['correct_infeasible']['denominator'] == summary['correct_infeasible']['missing'] == 1
    assert summary['numerical_fidelity']['denominator'] == 2


def test_unknown_semantics_count_separately_and_not_as_success():
    reference = case()
    changed = deepcopy(reference['problem'])
    changed['resource_unit'] = 'unknown-unit'
    header = plan([reference], methods=('json',))
    summary = summarize(header, [record(header, 0, output(reference, changed))], [reference])['methods']['json']
    assert summary['semantic_fidelity'] == dict(count=0, denominator=1, observed=1, unknown=1, missing=0)


def test_repetitions_do_not_inflate_primary_pairs_or_confidence_intervals():
    reference = case()
    header = plan([reference], methods=('json', 'structured'), repetitions=3)
    rows = [record(header, index, output(reference) if item['method'] == 'structured' else failed(reference))
            for index, item in enumerate(header['planned'])]
    summary = summarize(header, rows, [reference])
    assert summary['methods']['structured']['final_answer_correct']['denominator'] == 1
    assert summary['all_observations_descriptive']['structured']['final_answer_correct']['denominator'] == 3
    paired = summary['paired']['json/structured']
    assert paired['observed_pairs'] == 1 and paired['right_only'] == ['one']
    assert paired['exact_mcnemar_p'] == 1.0
    assert summary['repetition_status'] == {'1': 'complete', '2': 'complete', '3': 'complete'}


def test_conditional_repetitions_and_incomplete_pairs_are_explicit():
    reference = case()
    header = plan([reference], methods=('json', 'structured'), repetitions=3)
    header['repetitions_condition'] = 'Only when frozen resource conditions permit.'
    summary = summarize(header, [record(header, 0, output(reference))], [reference])
    assert summary['repetition_status']['2'] == 'planned_conditionally_not_started'
    assert summary['methods']['json']['missing'] == 0 and summary['methods']['structured']['missing'] == 1
    assert summary['paired']['json/structured']['missing_pairs'] == ['one']
    assert summary['paired']['json/structured']['observed_pairs'] == 0


@pytest.mark.parametrize('tamper', ['duplicate', 'identity', 'repetition', 'model'])
def test_result_identity_is_checked_before_summary(tamper):
    reference = case()
    header = plan([reference], methods=('json',))
    header['planned'][0]['model'] = 'model-one'
    row = dict(record(header, 0, output(reference)))
    rows = [row]
    if tamper == 'duplicate':
        rows.append(deepcopy(row))
    elif tamper == 'identity':
        row['case_id'] = 'another'
    elif tamper == 'repetition':
        row['repetition'] = 2
    else:
        row['model'] = 'other-model'
    with pytest.raises(ValueError):
        summarize(header, rows, [reference])


def test_exact_binomial_and_wilson_small_known_values():
    assert exact_paired_p(0, 0) == 1
    assert exact_paired_p(0, 6) == pytest.approx(.03125)
    assert exact_paired_p(4, 4) == 1
    assert wilson(0, 0) is None
    low, high = wilson(5, 10)
    assert low == pytest.approx(.236593090512564)
    assert high == pytest.approx(.763406909487436)
