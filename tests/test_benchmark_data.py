"""Audit frozen labels with exhaustive arithmetic independent of CP-SAT."""

import itertools
import json
from pathlib import Path
import re

import pytest

from or_agent.models import Gap


BENCHMARK = json.loads(Path("data/benchmark.json").read_text(encoding="utf-8"))
CASES = BENCHMARK["cases"]


def enumerate_reference(problem):
    feasible_costs = []
    resource_count = len(problem["resources"])
    task_count = len(problem["tasks"])
    for owners in itertools.product(range(resource_count), repeat=task_count):
        used = [0] * resource_count
        cost = 0
        for task, owner in enumerate(owners):
            used[owner] += problem["consumption"][owner][task]
            cost += problem["costs"][owner][task]
        if all(used[resource] <= problem["capacities"][resource]
               for resource in range(resource_count)):
            feasible_costs.append(cost)
    return (min(feasible_costs) if feasible_costs else None), len(feasible_costs)


def test_protocol_split_and_family_separation():
    assert BENCHMARK["protocol_version"] == "1"
    assert len(CASES) == 16
    ids = [case["id"] for case in CASES]
    assert len(set(ids)) == len(ids)
    assert all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", identifier) for identifier in ids)
    dev = [case for case in CASES if case["split"] == "dev"]
    test = [case for case in CASES if case["split"] == "test"]
    assert len(dev) == 7 and len(test) == 9
    assert {case["disposition"] for case in dev} == {"ready", "clarify", "unsupported", "invalid"}
    assert {case["family"] for case in dev}.isdisjoint(case["family"] for case in test)
    assert len({case["source"] for case in CASES}) == len(CASES)
    assert {case["category"] for case in test} == {
        "binding_capacities", "greedy_trap", "negative_zero", "multiple_optima",
        "infeasible", "ambiguous", "incomplete", "unsupported", "invalid"}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_reference_labels_and_complete_instance_domains(case):
    assert isinstance(case["source"], str) and case["source"].strip()
    assert len(case["source"]) <= 8000
    assert case["family"] and case["category"]
    assert case["disposition"] in {"ready", "clarify", "unsupported", "invalid"}
    if case["disposition"] == "ready":
        problem = Gap.model_validate(case["problem"]).model_dump()
        optimum, feasible_count = enumerate_reference(problem)
        assert case["reference_optimum"] == optimum
        assert case["reference_feasible_count"] == feasible_count
    else:
        assert case["problem"] is None
        assert case["reference_optimum"] is None
        assert case["reference_feasible_count"] is None


def test_numerical_instances_do_not_cross_development_and_test():
    signatures = {"dev": set(), "test": set()}
    for case in CASES:
        if case["problem"] is not None:
            problem = case["problem"]
            numerical_data = {field: problem[field] for field in ("costs", "consumption", "capacities")}
            signatures[case["split"]].add(json.dumps(numerical_data, sort_keys=True))
    assert signatures["dev"].isdisjoint(signatures["test"])
