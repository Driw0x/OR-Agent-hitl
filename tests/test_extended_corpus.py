"""Check frozen references, textual facts, and leakage without any model calls."""

from copy import deepcopy
from pathlib import Path

import pytest

from or_agent.extended_corpus import audit, seal_case, semantic_signature, sha
from or_agent.models import load_json


@pytest.fixture(scope="module")
def corpus():
    return load_json(Path("data/extended-benchmark.json").read_text(encoding="utf-8"))


def reseal(data):
    data["cases"] = [seal_case(case) for case in data["cases"]]
    data["content_sha256"] = sha(data["cases"])
    return data


def test_all_texts_references_and_solver_witnesses_agree(corpus):
    historical = load_json(Path("data/benchmark.json").read_text(encoding="utf-8"))
    report = audit(corpus, historical)
    assert report["valid"]
    assert report["development_count"] == 30 and report["test_count"] == 100
    assert report["reference_checks"] == report["cp_sat_checks"] == report["source_roundtrips"] == 105
    assert report["historical_cases_checked"] == 16
    assert report["test_special_counts"] == dict(ambiguous=5, incomplete=5, invalid=5, unsupported=5, infeasible=5)


def test_semantic_identity_ignores_names_and_both_orders(corpus):
    original = corpus["cases"][0]["problem"]
    reordered = deepcopy(original)
    reordered["tasks"] = [f"Job{i}" for i in range(len(original["tasks"]))]
    reordered["resources"] = [f"Worker{i}" for i in range(len(original["resources"]))]
    reordered["capacities"] = list(reversed(original["capacities"]))
    for field in ("costs", "consumption"):
        reordered[field] = [list(reversed(row)) for row in reversed(original[field])]
    assert semantic_signature(original) == semantic_signature(reordered)
    reordered["costs"][0][0] += 17
    assert semantic_signature(original) != semantic_signature(reordered)


@pytest.mark.parametrize("tamper", ["text", "optimum", "count", "family", "duplicate", "hash"])
def test_audit_rejects_corrupted_or_leaked_cases(corpus, tamper):
    changed = deepcopy(corpus)
    first = changed["cases"][0]
    if tamper == "text":
        first["source"] = first["source"].replace("Cumulative capacity of DomeA: ", "Cumulative capacity of DomeA: 1", 1)
    elif tamper == "optimum":
        first["reference_optimum"] += 1
    elif tamper == "count":
        first["reference_feasible_count"] += 1
    elif tamper == "family":
        changed["cases"][30]["family"] = first["family"]
    elif tamper == "duplicate":
        changed["cases"][1]["source"] = first["source"]
        changed["cases"][1]["problem"] = first["problem"]
    if tamper != "hash":
        reseal(changed)
    else:
        first["source"] += "\n"
    with pytest.raises(ValueError):
        audit(changed, compare_solver=False)


def test_ready_special_cases_are_genuinely_infeasible(corpus):
    impossible = [case for case in corpus["cases"] if case["category"] == "special" and case["disposition"] == "ready"]
    assert len(impossible) == 7
    assert all(case["reference_optimum"] is None and case["reference_feasible_count"] == 0 for case in impossible)
    test_shapes = {(len(case["problem"]["tasks"]), len(case["problem"]["resources"]))
                   for case in impossible if case["split"] == "test"}
    assert len(test_shapes) == 5
