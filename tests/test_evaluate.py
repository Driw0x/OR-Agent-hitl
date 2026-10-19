"""Synthetic reference/trace audits; no network, LLM generation or benchmark corpus."""

from copy import deepcopy
import json

import pytest

from or_agent.agent import answer_for
from or_agent.evaluate import assess, audit, canonical, digest, enumerate_reference, freeze, replay, summarize
from or_agent.models import Gap
from or_agent.verify import verify


def problem():
    return dict(tasks=["T1", "T2"], resources=["R1", "R2"],
                costs=[[1, 8], [4, 2]], consumption=[[1, 1], [1, 1]],
                capacities=[1, 1], cost_unit="EUR", resource_unit="slots")


def permuted(data):
    result = deepcopy(data)
    for key in ("tasks", "resources", "capacities"):
        result[key].reverse()
    for key in ("costs", "consumption"):
        result[key] = [list(reversed(row)) for row in reversed(result[key])]
    return result


def reference(case_id="case1", split="test", family="family1", data=None):
    data = deepcopy(data) if data is not None else problem()
    answer = enumerate_reference(data)
    return dict(id=case_id, split=split, family=family, source=f"Explicit problem {case_id}",
                disposition="ready", problem=data, reference_optimum=answer["optimum"],
                reference_feasible_count=answer["feasible_count"])


def verified(data=None):
    data = deepcopy(data) if data is not None else problem()
    candidate = dict(assignments=[dict(task="T1", resource="R1"), dict(task="T2", resource="R2")],
                     objective=data["costs"][0][0] + data["costs"][1][1])
    result = dict(outcome="verified", reason="independent_check_passed", candidate=candidate,
                  optimality_proven=True,
                  extraction=dict(disposition="ready", problem=data, issues=[], provenance={}),
                  verification=verify(Gap.model_validate(data), candidate),
                  solver=dict(solver_proof=True, raw_status="OPTIMAL", objective=candidate["objective"],
                              best_bound=candidate["objective"],
                              parameters=dict(absolute_gap_limit=0, relative_gap_limit=0),
                              solver_wall_seconds=0.001),
                  attempts=[dict(json_valid=True, structural_valid=True, raw="synthetic JSON",
                                 response=dict(prompt_eval_count=11, eval_count=7,
                                               load_duration=2_000_000_000, eval_duration=3_000_000_000))],
                  observations=[dict(tool=name) for name in ("validate", "solve", "verify")],
                  elapsed_seconds=0.01)
    result["answer"] = answer_for(result)
    return result


def failed():
    result = dict(outcome="execution_error", reason="synthetic timeout", candidate=None,
                  optimality_proven=False, extraction=None,
                  attempts=[dict(json_valid=False, structural_valid=False, raw=None, response=None)],
                  observations=[], elapsed_seconds=1.0)
    result["answer"] = answer_for(result)
    return result


def header(cases, methods=("json",)):
    return dict(methods=list(methods), planned=[
        dict(case_id=case["id"], method=method, ready_reference=case["disposition"] == "ready",
             feasible_reference=case["disposition"] == "ready" and case["reference_optimum"] is not None,
             infeasible_reference=case["disposition"] == "ready" and case["reference_optimum"] is None)
        for case in cases for method in methods])


def record(plan, index, case, result):
    return dict(kind="result", index=index, **plan["planned"][index],
                result=result, metrics=assess(case, result))


def traces(tmp_path, include_second=False):
    cases = [reference(), reference("case2", family="family2")]
    data_path = tmp_path / "synthetic.json"
    data_path.write_text(json.dumps(dict(cases=cases)), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(dict(dataset=str(data_path), dataset_sha256=digest(data_path))), encoding="utf-8")
    plan = header(cases)
    plan.update(kind="campaign", manifest=str(manifest_path), manifest_sha256=digest(manifest_path))
    records = [plan, record(plan, 0, cases[0], verified())]
    if include_second:
        records.append(record(plan, 1, cases[1], failed()))
    path = tmp_path / "records.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
    return path, records, data_path


@pytest.mark.parametrize('tampered', ['manifest', 'dataset'])
def test_replay_relocates_sealed_paths_without_weakening_hashes(tmp_path, monkeypatch, tampered):
    from or_agent import evaluate

    path, rows, data_path = traces(tmp_path)
    expected = replay(path)
    (tmp_path / 'data').mkdir()
    (tmp_path / 'experiments').mkdir()
    dataset = tmp_path / 'data/synthetic.json'
    data_path.replace(dataset)
    manifest = tmp_path / 'experiments/manifest.json'
    manifest.write_text(json.dumps(dict(dataset='Z:/archive/OR-Agent/data/synthetic.json',
                                       dataset_sha256=digest(dataset))), encoding='utf-8')
    rows[0].update(manifest='Z:/archive/OR-Agent/experiments/manifest.json', manifest_sha256=digest(manifest))
    path.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    monkeypatch.setattr(evaluate, '__file__', str(tmp_path / 'or_agent/evaluate.py'))
    assert replay(path) == expected
    target = manifest if tampered == 'manifest' else dataset
    target.write_bytes(target.read_bytes() + b' ')
    with pytest.raises(ValueError, match='provenance hash mismatch'):
        replay(path)


def test_canonical_preserves_problem_under_row_column_permutations():
    data = problem()
    assert canonical(data) == canonical(permuted(data))
    changed = deepcopy(data)
    changed["costs"][0][0] += 1
    assert canonical(data) != canonical(changed)


def test_exhaustive_reference_and_audit():
    case = reference()
    assert enumerate_reference(case["problem"]) == dict(optimum=3, feasible_count=2, optimum_count=1)
    checked = audit(dict(cases=[case]))
    assert checked["valid"] and checked["test_count"] == 1 and checked["dev_count"] == 0


@pytest.mark.parametrize("key,value", [("reference_optimum", 4), ("reference_feasible_count", 3)])
def test_audit_rejects_false_reference(key, value):
    case = reference()
    case[key] = value
    with pytest.raises(ValueError, match="incorrect reference"):
        audit(dict(cases=[case]))


@pytest.mark.parametrize("kind", ["family", "source", "formulation", "permuted_formulation"])
def test_audit_rejects_cross_split_leakage(kind):
    development = reference("dev1", split="dev", family="development")
    test = reference("test1", family="held_out")
    if kind == "family":
        test["family"] = development["family"]
    elif kind == "source":
        test["source"] = development["source"]
        test["problem"]["costs"][0][0] += 1
    elif kind == "permuted_formulation":
        test["problem"] = permuted(test["problem"])
    with pytest.raises(ValueError, match="leakage"):
        audit(dict(cases=[development, test]))


@pytest.mark.parametrize("key,value", [("split", "training"), ("disposition", "guess")])
def test_audit_rejects_unknown_protocol_labels(key, value):
    case = reference()
    case[key] = value
    with pytest.raises(ValueError):
        audit(dict(cases=[case]))


def test_audit_rejects_duplicate_case_identity():
    case = reference()
    with pytest.raises(ValueError, match="duplicate case"):
        audit(dict(cases=[case, deepcopy(case)]))


def test_reference_assessment_valid_success():
    metrics = assess(reference(), verified())
    assert metrics["faithful"] and metrics["verified_feasible"] and metrics["proven_optimal"]
    assert metrics["answer_correct"] and metrics["reference_check"]["valid"]
    assert not metrics["false_validity_claim"] and not metrics["false_optimality_claim"]
    assert metrics["llm_calls"] == 1 and metrics["tool_calls"] == 3
    assert metrics["input_tokens"] == 11 and metrics["output_tokens"] == 7


def test_wrong_formulation_passes_local_checker_but_not_reference():
    changed = problem()
    changed["costs"] = [[1, 1], [1, 1]]
    output = verified(changed)
    assert output["verification"]["valid"]
    metrics = assess(reference(), output)
    assert not metrics["faithful"] and not metrics["verified_feasible"]
    assert not metrics["answer_correct"] and metrics["reference_answer_mismatch"]
    assert metrics["reference_violation_count"] == 1
    assert not metrics["false_validity_claim"] and not metrics["false_optimality_claim"]
    assert metrics["error_category"] == "extraction_or_translation"


@pytest.mark.parametrize("tamper", ["status", "bound", "gap", "candidate"])
def test_recorded_flags_do_not_create_validity_or_proof(tamper):
    output = verified()
    if tamper == "status":
        output["solver"]["raw_status"] = "FEASIBLE"
    elif tamper == "bound":
        output["solver"]["best_bound"] -= 1
    elif tamper == "gap":
        output["solver"]["parameters"]["relative_gap_limit"] = 0.1
    else:
        output["candidate"]["objective"] = 0
    output["answer"] = answer_for(output)
    metrics = assess(reference(), output)
    assert not metrics["proven_optimal"] and metrics["false_optimality_claim"]
    assert not metrics["explanation_consistent"]
    if tamper == "candidate":
        assert metrics["false_validity_claim"]


def test_missing_token_measurements_stay_null_and_failed_attempts_count():
    output = verified()
    output["attempts"].insert(0, dict(json_valid=False, structural_valid=False, raw=None, response=None))
    metrics = assess(reference(), output)
    assert metrics["llm_calls"] == 2
    for key in ("input_tokens", "output_tokens", "load_seconds", "generation_seconds"):
        assert metrics[key] is None


def test_missing_run_preserves_planned_denominators():
    feasible = reference()
    impossible_data = problem()
    impossible_data["capacities"] = [0, 0]
    impossible = reference("impossible", family="impossible", data=impossible_data)
    plan = header([feasible, impossible])
    result = summarize(plan, [record(plan, 0, feasible, verified())])["methods"]["json"]
    assert (result["planned"], result["completed"], result["missing"]) == (2, 1, 1)
    assert result["answer_correct"] == dict(count=1, denominator=2)
    assert result["faithful"] == dict(count=1, denominator=2)
    assert result["proven_optimal"] == dict(count=1, denominator=1)
    assert result["correct_infeasible"] == dict(count=0, denominator=1)


def test_failed_runs_are_retained_without_exclusion():
    cases = [reference(), reference("case2", family="family2")]
    plan = header(cases)
    rows = [record(plan, 0, cases[0], verified()), record(plan, 1, cases[1], failed())]
    result = summarize(plan, rows)["methods"]["json"]
    assert result["completed"] == 2 and result["missing"] == 0
    assert result["answer_correct"] == dict(count=1, denominator=2)
    assert result["outcomes"] == dict(execution_error=1, verified=1)
    assert result["errors"] == dict(execution_error=1)
    assert result["input_tokens"]["available"] == 1
    assert result["elapsed_seconds"]["sum"] == 1.01


@pytest.mark.parametrize("no_results", [False, True])
def test_all_missing_token_summaries_remain_null(no_results):
    case = reference()
    plan = header([case])
    rows = [] if no_results else [record(plan, 0, case, failed())]
    result = summarize(plan, rows)["methods"]["json"]
    for key in ("input_tokens", "output_tokens", "load_seconds", "generation_seconds"):
        assert result[key] == dict(sum=None, mean=None, min=None, max=None, available=0)


def test_replay_recomputes_offline_and_reports_incomplete_plan(tmp_path):
    path, _, _ = traces(tmp_path)
    result = replay(path)["methods"]["json"]
    assert result["answer_correct"] == dict(count=1, denominator=2)
    assert result["completed"] == 1 and result["missing"] == 1


@pytest.mark.parametrize("tamper", ["metrics", "duplicate", "index", "case_id", "method"])
def test_replay_rejects_tampered_or_unplanned_results(tmp_path, tamper):
    path, records, _ = traces(tmp_path)
    if tamper == "metrics":
        records[1]["metrics"]["answer_correct"] = False
    elif tamper == "duplicate":
        records.append(deepcopy(records[1]))
    elif tamper == "index":
        records[1]["index"] = 99
    elif tamper == "case_id":
        records[1]["case_id"] = "case2"
    else:
        records[1]["method"] = "structured"
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        replay(path)


def test_replay_rejects_modified_reference_dataset(tmp_path):
    path, _, data_path = traces(tmp_path)
    data_path.write_text(data_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="provenance hash"):
        replay(path)


def test_freeze_checks_references_and_refuses_overwrite(tmp_path, monkeypatch):
    from io import BytesIO
    from or_agent.agent import MODEL

    data_path = tmp_path / "synthetic.json"
    data_path.write_text(json.dumps(dict(cases=[reference()])), encoding="utf-8")
    hardware = {"ollama": {"models": [{"name": MODEL, "digest": "synthetic-model-digest"}]}}
    monkeypatch.setattr("or_agent.evaluate.snapshot", lambda: hardware)
    monkeypatch.setattr("or_agent.evaluate.urlopen", lambda *args, **kwargs: BytesIO(b'{"version":"synthetic"}'))
    manifest_path = tmp_path / "manifest.json"
    manifest = freeze(data_path, manifest_path)
    assert manifest["dataset_sha256"] == digest(data_path)
    assert manifest["model_digest"] == "synthetic-model-digest"
    assert manifest["methods"] == ["json", "structured", "structured-repair"] and manifest["repetitions"] == 1
    assert manifest["audit"]["valid"]
    original = manifest_path.read_bytes()
    with pytest.raises(FileExistsError):
        freeze(data_path, manifest_path)
    assert manifest_path.read_bytes() == original
