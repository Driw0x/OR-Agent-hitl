"""Controlled client responses test autonomy without contacting Ollama."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from or_agent import agent


def source_and_extraction():
    problem = json.loads(Path("examples/small.json").read_text())
    source = "Assign every task once and minimize cost. " + json.dumps(problem)
    extraction = dict(disposition="ready", problem=problem, issues=[],
                      provenance={field: source for field in problem})
    return source, extraction


def controlled_client(*outputs):
    requests = []
    responses = iter(outputs)

    def client(payload, remaining):
        assert remaining > 0
        requests.append(deepcopy(payload))
        output = next(responses)
        if isinstance(output, Exception):
            raise output
        raw = output if isinstance(output, str) else json.dumps(output)
        return {"message": {"content": raw}}

    return client, requests


def execute(source, *outputs, **parameters):
    client, requests = controlled_client(*outputs)
    result = agent.run(source, client=client, admission=lambda: {"allowed": True},
                       **parameters)
    return result, requests


def test_verified_end_to_end_retains_source_and_evidence():
    source, extraction = source_and_extraction()
    result, requests = execute(source, extraction)
    assert result["outcome"] == "verified"
    assert result["candidate"]["objective"] == 6
    assert result["verification"]["valid"]
    assert result["verification"]["loads"] == {"R1": 4, "R2": 2}
    assert result["optimality_proven"]
    assert result["fidelity"] == "unassessed"
    assert result["source"] == source
    assert result["extraction"] == extraction
    assert len(result["attempts"]) == len(requests) == 1
    assert result["attempts"][0]["json_valid"]
    assert result["attempts"][0]["structural_valid"]
    assert [item["tool"] for item in result["observations"]] == [
        "resource_gate", "validate", "solve", "verify"]
    assert "extracted model" in result["answer"]
    assert "unassessed" in result["answer"]
    assert result["explanation_valid"]


def test_cli_defaults_to_structured_repair_without_strategy_option(monkeypatch, capsys):
    from or_agent.cli import main

    monkeypatch.setattr('sys.argv', ['or-agent', 'agent', 'examples/small.txt',
                                   '--total-seconds', '0', '--model', 'controlled-model'])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result['method'] == 'structured-repair'
    assert result['budgets']['max_calls'] == 2
    assert result['model'] == 'controlled-model'
    assert result['attempts'] == []


@pytest.mark.parametrize("source,disposition,issue", [
    ("Assign T1, but capacity could mean 2 or 3 slots.", "clarify", "Which capacity applies?"),
    ("Assign T1 to R1; no capacity is supplied.", "clarify", "Supply capacity and units."),
    ("Assign T1 to R1 before a deadline of 5 seconds.", "unsupported", "Deadlines are outside GAP."),
    ("Capacity R1 is both 2 and 3 slots.", "invalid", "Contradictory capacities."),
])
def test_nonready_dispositions_stop_without_solving(source, disposition, issue):
    output = dict(disposition=disposition, problem=None, issues=[issue], provenance={})
    result, requests = execute(source, output)
    assert result["outcome"] == disposition
    assert result["reason"] == issue
    assert result["candidate"] is None
    assert not result["optimality_proven"]
    assert "solver" not in result
    assert len(requests) == 1
    assert result["explanation_valid"]


def test_infeasibility_stops_without_correction_or_assignment():
    source, extraction = source_and_extraction()
    extraction["problem"]["capacities"] = [1, 1]
    source = json.dumps(extraction["problem"])
    extraction["provenance"] = {field: source for field in extraction["problem"]}
    result, requests = execute(source, extraction)
    assert result["outcome"] == "infeasible"
    assert result["solver"]["raw_status"] == "INFEASIBLE"
    assert result["candidate"] is None
    assert len(requests) == 1
    assert "extracted model infeasible" in result["answer"]
    assert result["explanation_valid"]


def test_malformed_json_receives_bounded_diagnostic_repair():
    source, extraction = source_and_extraction()
    malformed = json.dumps(extraction)[:-1]
    result, requests = execute(source, malformed, extraction)
    assert result["outcome"] == "verified"
    assert len(requests) == len(result["attempts"]) == 2
    assert not result["attempts"][0]["json_valid"]
    assert result["attempts"][0]["error"]
    assert result["attempts"][1]["structural_valid"]
    assert requests[1]["messages"][1]["content"] == source
    assert requests[1]["messages"][-2] == {"role": "assistant", "content": malformed}
    assert "Diagnostic:" in requests[1]["messages"][-1]["content"]
    assert "Preserve source" in requests[1]["messages"][-1]["content"]
    assert result["fidelity"] == "unassessed"


def test_missing_provenance_repair_keeps_every_problem_datum():
    source, extraction = source_and_extraction()
    broken = deepcopy(extraction)
    del broken["provenance"]["capacities"]
    result, requests = execute(source, broken, extraction)
    assert result["outcome"] == "verified"
    assert len(requests) == 2
    assert result["attempts"][0]["json_valid"]
    assert not result["attempts"][0]["structural_valid"]
    assert "provenance" in result["attempts"][0]["error"]
    assert result["extraction"]["problem"] == broken["problem"]


def test_correction_changing_locked_capacity_abstains():
    source, extraction = source_and_extraction()
    broken = deepcopy(extraction)
    broken["provenance"] = {}
    changed = deepcopy(extraction)
    changed["problem"]["capacities"][0] = 100
    result, requests = execute(source, broken, changed)
    assert len(requests) == 2
    assert result["outcome"] == "abstain"
    assert result["reason"] == "correction_changes_locked_data"
    assert result["candidate"] is None
    assert "solver" not in result
    assert result["attempts"][1]["error"] == result["reason"]


def test_correction_removing_unknown_deadline_abstains():
    source, extraction = source_and_extraction()
    broken = deepcopy(extraction)
    broken["problem"]["deadline"] = 5
    result, requests = execute(source, broken, extraction)
    assert len(requests) == 2
    assert "deadline" in result["attempts"][0]["error"]
    assert result["outcome"] == "abstain"
    assert result["reason"] == "correction_changes_locked_data"
    assert result["candidate"] is None
    assert "solver" not in result


def test_unknown_root_constraint_cannot_be_removed_by_repair():
    source, extraction = source_and_extraction()
    broken = deepcopy(extraction)
    broken["deadline"] = 5
    result, requests = execute(source, broken, extraction)
    assert len(requests) == 1
    assert result["outcome"] == "abstain"
    assert result["reason"] == "unknown_extraction_fields_cannot_be_removed"
    assert result["candidate"] is None
    assert "solver" not in result
    assert result["attempts"][0]["error"] == result["reason"]


def test_repeated_invalid_output_exhausts_call_budget():
    source, _ = source_and_extraction()
    result, requests = execute(source, "{", "{")
    assert len(requests) == len(result["attempts"]) == 2
    assert result["outcome"] == "abstain"
    assert result["reason"] == "invalid_extraction_call_budget_exhausted"
    assert all(attempt["error"] for attempt in result["attempts"])
    assert result["candidate"] is None


@pytest.mark.parametrize("parameters,reason", [
    ({"max_calls": 0}, "call_budget_exhausted"),
    ({"total_seconds": 0}, "elapsed_budget_exhausted"),
])
def test_zero_budget_makes_no_client_request(parameters, reason):
    source, _ = source_and_extraction()
    result, requests = execute(source, **parameters)
    assert result["outcome"] == "abstain"
    assert result["reason"] == reason
    assert result["candidate"] is None
    assert requests == result["attempts"] == []


def test_transport_timeout_preserves_failure_without_retry():
    source, _ = source_and_extraction()
    result, requests = execute(source, TimeoutError("controlled transport timeout"))
    assert result["outcome"] == "execution_error"
    assert result["reason"] == "TimeoutError: controlled transport timeout"
    assert len(requests) == 1
    assert result["attempts"][0]["response"] is None
    assert result["attempts"][0]["error"] == result["reason"]
    assert result["candidate"] is None
    assert result["explanation_valid"]


def test_checker_rejection_never_emits_assignment(monkeypatch):
    source, extraction = source_and_extraction()
    checked = dict(valid=False, violations=[dict(kind="injected_checker_failure")])
    monkeypatch.setattr(agent, "verify", lambda problem, candidate: checked)
    result, requests = execute(source, extraction)
    assert result["outcome"] == "abstain"
    assert result["reason"] == "checker_rejected"
    assert result["verification"] == checked
    assert result["candidate"] is None
    assert not result["optimality_proven"]
    assert len(requests) == 1


def test_false_explanation_and_unsupported_proof_are_detected():
    source, extraction = source_and_extraction()
    result, _ = execute(source, extraction)
    forged = deepcopy(result)
    forged["answer"] = "This proves the original user problem has optimal cost zero."
    assert not agent.explanation_valid(forged)
    forged = deepcopy(result)
    forged["solver"]["solver_proof"] = False
    assert not agent.explanation_valid(forged)
    forged = deepcopy(result)
    forged["verification"]["valid"] = False
    assert not agent.explanation_valid(forged)


@pytest.mark.parametrize("method,has_schema", [("json", False), ("structured", True), ("structured-repair", True)])
def test_generation_schema_distinguishes_baselines(method, has_schema):
    source, extraction = source_and_extraction()
    result, requests = execute(source, extraction, method=method)
    assert result["outcome"] == "verified"
    assert ("format" in requests[0]) is has_schema
    assert result["budgets"]["max_calls"] == (2 if method == "structured-repair" else 1)


@pytest.mark.parametrize("method", ["json", "structured"])
def test_single_call_baselines_never_receive_repair_feedback(method):
    source, extraction = source_and_extraction()
    result, requests = execute(source, "{", extraction, method=method)
    assert len(requests) == 1
    assert result["outcome"] == "abstain"
    assert result["reason"] == "invalid_extraction_call_budget_exhausted"


def test_resource_refusal_prevents_generation():
    source, _ = source_and_extraction()
    client, requests = controlled_client()
    result = agent.run(source, client=client,
                       admission=lambda: {"allowed": False, "reason": "controlled_ram_pressure"})
    assert result["outcome"] == "resource_blocked"
    assert result["reason"] == "controlled_ram_pressure"
    assert requests == result["attempts"] == []
    assert result["observations"][0]["tool"] == "resource_gate"


def test_invalid_quote_repair_preserves_source_costs_and_objective():
    source, extraction = source_and_extraction()
    broken = deepcopy(extraction)
    broken['provenance']['costs'] = 'This quotation does not occur in the source.'
    result, requests = execute(source, broken, extraction)
    assert result['outcome'] == 'verified' and len(requests) == 2
    assert 'provenance for costs' in result['attempts'][0]['error']
    assert result['source'] == source
    assert result['extraction']['problem'] == broken['problem']
    assert result['candidate']['objective'] == result['verification']['recomputed_objective'] == 6


def test_correction_changing_locked_costs_abstains():
    source, extraction = source_and_extraction()
    broken = deepcopy(extraction)
    broken['provenance'] = {}
    changed = deepcopy(extraction)
    changed['problem']['costs'][0][0] = 0
    result, requests = execute(source, broken, changed)
    assert len(requests) == 2 and result['source'] == source
    assert result['outcome'] == 'abstain' and result['reason'] == 'correction_changes_locked_data'
    assert result['candidate'] is None and 'solver' not in result
    assert json.loads(result['attempts'][0]['raw'])['problem'] == broken['problem']


def test_locked_problem_feedback_explains_why_null_clarification_is_forbidden():
    source, extraction = source_and_extraction()
    broken = deepcopy(extraction)
    broken['problem']['capacities'].append(99)
    clarification = dict(disposition='clarify', problem=None,
                         issues=['Clarify the resource and capacity counts.'], provenance={})
    result, requests = execute(source, broken, clarification)
    assert result['outcome'] == 'abstain' and result['reason'] == 'correction_changes_locked_data'
    assert result['candidate'] is None and 'solver' not in result
    feedback = requests[1]['messages'][-1]['content']
    assert 'If a change is needed, ask clarification instead.' not in feedback
    assert 'problem=null' in feedback and 'locked' in feedback.lower()


def test_malformed_json_without_locked_problem_can_request_clarification():
    source = 'Assign task Check once to resource Desk. The capacity and units are missing; ask for them.'
    clarification = dict(disposition='clarify', problem=None,
                         issues=['What capacity and units should Desk use?'], provenance={})
    result, requests = execute(source, '{', clarification)
    assert result['outcome'] == 'clarify' and len(requests) == 2
    assert result['source'] == source and result['extraction'] == clarification
    assert result['candidate'] is None and 'solver' not in result


def test_infeasibility_is_only_for_extracted_model():
    from or_agent.evaluate import enumerate_reference
    from or_agent.extended_metrics import assess
    from or_agent.models import Gap
    from or_agent.solver import solve
    from or_agent.verify import verify
    corpus = json.loads(Path('data/extended-benchmark.json').read_text(encoding='utf-8'))
    case = next(c for c in corpus['cases'] if c['id'] == 'dev_simple_02')
    extraction = dict(disposition='ready', problem=deepcopy(case['problem']), issues=[],
                      provenance={field: case['source'] for field in case['problem']})
    extraction['problem']['consumption'][1][2] = 4
    reference = solve(Gap.model_validate(case['problem']))
    assert reference['objective'] == 38 and reference['solver_proof']
    assert verify(Gap.model_validate(case['problem']), reference['candidate'])['valid']
    assert enumerate_reference(case['problem'])['feasible_count'] == 1
    assert enumerate_reference(extraction['problem'])['feasible_count'] == 0
    result, requests = execute(case['source'], extraction, method='structured')
    assert result['outcome'] == 'infeasible' and result['solver']['raw_status'] == 'INFEASIBLE'
    assert result['answer'] == (
        'CP-SAT proves the extracted model infeasible. Source fidelity is unassessed.')
    assert result['fidelity'] == 'unassessed' and result['explanation_valid']
    assert result['candidate'] is None and len(requests) == 1
    metrics = assess(case, result)
    assert not metrics['final_answer_correct'] and not metrics['correct_infeasible']
    assert metrics['reference_answer_mismatch'] and not metrics['false_guarantee']


@pytest.mark.parametrize('status,outcome,error', [('UNKNOWN', 'solver', None),
    ('MODEL_INVALID', 'solver', None), (None, 'execution_error', 'controlled solver failure')])
def test_inconclusive_solver_results_never_claim_infeasibility(monkeypatch, status, outcome, error):
    source, extraction = source_and_extraction()
    solved = dict(raw_status=status, outcome=outcome, candidate=None, solver_proof=False)
    if error: solved['error'] = error
    monkeypatch.setattr(agent, 'solve', lambda *args: solved)
    result, requests = execute(source, extraction)
    assert result['outcome'] == 'abstain' and result['candidate'] is None
    assert result['solver'] == solved and not result['optimality_proven']
    assert result['reason'] == (error or status)
    assert 'No verified assignment' in result['answer'] and 'infeasible' not in result['answer']
    assert result['explanation_valid'] and len(requests) == 1
