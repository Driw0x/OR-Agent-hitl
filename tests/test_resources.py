"""Admission policy checks use fake snapshots and never create resource pressure."""

from copy import deepcopy
import json

import pytest

from or_agent.resources import GIB, WaitBudget, gate, snapshot


def healthy():
    return {"ram_total_bytes": 32 * GIB, "ram_available_bytes": 20 * GIB,
            "commit_limit_bytes": 48 * GIB, "commit_available_bytes": 32 * GIB,
            "cpu_percent": 20, "disk_free_bytes": 100 * GIB,
            "ollama": {"available": True, "models": [{"name": "tiny:latest", "size": 4 * GIB}],
                       "loaded_models": []}}


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr("or_agent.resources.time.monotonic", lambda: now[0])
    return sleep, sleeps


def test_immediate_admission_and_model_estimate(clock):
    sleep, sleeps = clock
    report = gate("tiny", snapshot_fn=healthy, sleep_fn=sleep)
    assert report["allowed"] and report["reason"] == "resources_available"
    assert report["estimated_bytes"] == (4 * GIB * 13 + 9) // 10 + GIB
    assert report["safety_margin_bytes"] == (32 * GIB + 9) // 10
    assert report["wait_seconds"] == 0 and sleeps == []
    json.dumps(report, allow_nan=False)


def test_resident_model_reserves_incremental_memory_only(clock):
    sleep, sleeps = clock
    state = healthy()
    state["ram_available_bytes"] = 5 * GIB
    state["ollama"]["loaded_models"] = [{"model": "tiny:latest", "size": 4 * GIB}]
    report = gate("tiny", snapshot_fn=lambda: state, sleep_fn=sleep)
    assert report["allowed"] and report["loaded_model_reused"]
    assert report["estimated_bytes"] == GIB
    assert report["full_estimated_bytes"] > 6 * GIB
    assert sleeps == []


def test_other_loaded_model_receives_no_residency_credit(clock):
    sleep, sleeps = clock
    state = healthy()
    state["ram_available_bytes"] = 5 * GIB
    state["ollama"]["loaded_models"] = [{"model": "other:latest", "size": 4 * GIB}]
    report = gate("tiny", wait_budget=WaitBudget(operation_seconds=0), snapshot_fn=lambda: state, sleep_fn=sleep)
    assert not report["allowed"] and not report["loaded_model_reused"]
    assert report["estimated_bytes"] == report["full_estimated_bytes"]
    assert state["ollama"]["loaded_models"][0]["model"] == "other:latest"
    assert sleeps == []


@pytest.mark.parametrize("field,value,resource", [
    ("ram_available_bytes", GIB, "ram"),
    ("commit_available_bytes", GIB, "commit"),
    ("cpu_percent", 95, "cpu"),
])
def test_temporary_pressure_waits_and_rechecks(field, value, resource, clock):
    sleep, sleeps = clock
    busy = healthy()
    busy[field] = value
    states = iter([busy, healthy()])
    report = gate("tiny", snapshot_fn=lambda: next(states), sleep_fn=sleep)
    assert report["allowed"] and report["wait_seconds"] == 60
    assert report["measured_wait_seconds"] == 60
    assert len(report["snapshots"]) == 2 and sleeps == [60]
    assert busy[field] == value


def test_operation_and_shared_campaign_caps(clock):
    sleep, sleeps = clock
    busy = healthy()
    busy["ram_available_bytes"] = GIB
    budget = WaitBudget()
    for index in range(3):
        report = gate("tiny", wait_budget=budget, snapshot_fn=lambda: deepcopy(busy), sleep_fn=sleep)
        assert not report["allowed"] and report["reason"] == "wait_budget_exhausted"
        assert report["limiting_resource"] == "ram"
        assert report["wait_seconds"] == 600 and len(report["snapshots"]) == 11
        assert budget.used_seconds == 600 * (index + 1)
    exhausted = gate("tiny", wait_budget=budget, snapshot_fn=lambda: busy, sleep_fn=sleep)
    assert exhausted["wait_seconds"] == 0 and len(exhausted["snapshots"]) == 1
    assert sum(sleeps) == 1800


@pytest.mark.parametrize("changes,expected", [
    ({"ram_total_bytes": 6 * GIB, "ram_available_bytes": 6 * GIB}, "structural_ram_limit"),
    ({"commit_limit_bytes": 8 * GIB, "commit_available_bytes": 8 * GIB}, "structural_commit_limit"),
    ({"ram_available_bytes": None}, "ram_unknown"),
    ({"commit_available_bytes": None}, "commit_unknown"),
    ({"disk_free_bytes": 0}, "disk_full"),
    ({"ollama": {"available": False}}, "ollama_unavailable"),
    ({"ollama": {"available": True, "models": []}}, "model_not_installed"),
    ({"ollama": {"available": True, "models": [{"name": "tiny", "size": None}]}}, "model_size_unknown"),
])
def test_direct_refusal_does_not_wait(changes, expected, clock):
    sleep, sleeps = clock
    state = healthy()
    state.update(changes)
    report = gate("tiny", snapshot_fn=lambda: state, sleep_fn=sleep)
    assert not report["allowed"] and report["reason"] == expected
    assert report["wait_seconds"] == 0 and sleeps == []


def test_partial_campaign_budget_and_zero_operation_budget(clock):
    sleep, sleeps = clock
    state = healthy()
    state["cpu_percent"] = 100
    budget = WaitBudget(used_seconds=1790)
    report = gate("tiny", wait_budget=budget, snapshot_fn=lambda: state, sleep_fn=sleep)
    assert not report["allowed"] and sleeps == [10]
    assert report["wait_seconds"] == 10 and budget.used_seconds == 1800
    report = gate("tiny", wait_budget=WaitBudget(operation_seconds=0), snapshot_fn=lambda: state, sleep_fn=sleep)
    assert not report["allowed"] and report["wait_seconds"] == 0


def test_oversleep_is_recorded_and_exhausts_bounded_budget(monkeypatch):
    now = [0.0]
    sleeps = []
    monkeypatch.setattr("or_agent.resources.time.monotonic", lambda: now[0])

    def oversleep(seconds):
        sleeps.append(seconds)
        now[0] += 2000

    state = healthy()
    state["cpu_percent"] = 100
    budget = WaitBudget()
    report = gate("tiny", wait_budget=budget, snapshot_fn=lambda: state, sleep_fn=oversleep)
    assert not report["allowed"] and sleeps == [60]
    assert report["wait_seconds"] == 600
    assert report["measured_wait_seconds"] == 2000
    assert report["wait_overrun_seconds"] == 1400
    assert budget.used_seconds == 1800


@pytest.mark.parametrize("parameters", [
    {"total_seconds": 1801}, {"operation_seconds": 601}, {"interval_seconds": 0},
    {"interval_seconds": 61}, {"total_seconds": float("inf")}, {"used_seconds": 1801},
    {"operation_seconds": True},
])
def test_invalid_wait_budgets(parameters):
    with pytest.raises(ValueError):
        WaitBudget(**parameters)


def test_snapshot_preserves_unknowns_and_queries_inventory(monkeypatch):
    from io import BytesIO

    monkeypatch.setattr("or_agent.resources._native_snapshot", lambda: {
        "ram_total_bytes": None, "ram_available_bytes": None, "diagnostics": ["native unavailable"]})
    calls = []

    def urlopen(url, timeout):
        calls.append((url, timeout))
        return BytesIO(json.dumps({"models": [{"name": "tiny", "size": 123}]}).encode())

    monkeypatch.setattr("or_agent.resources.urlopen", urlopen)
    report = snapshot("http://localhost:11434/")
    assert report["ram_total_bytes"] is None
    assert report["gpu"]["vram_total_bytes"] is None
    assert report["ollama"]["available"]
    assert report["ollama"]["loaded_models"][0]["name"] == "tiny"
    assert calls == [("http://localhost:11434/api/tags", 3), ("http://localhost:11434/api/ps", 3)]
    json.dumps(report, allow_nan=False)


def test_snapshot_retains_server_failure(monkeypatch):
    monkeypatch.setattr("or_agent.resources._native_snapshot", lambda: {"diagnostics": []})

    def unavailable(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("or_agent.resources.urlopen", unavailable)
    report = snapshot()
    assert not report["ollama"]["available"]
    assert report["ollama"]["loaded_models"] is None
    assert "connection refused" in report["ollama"]["error"]
    assert any("Ollama tags" in message for message in report["diagnostics"])


def test_elapsed_deadline_neither_samples_nor_sleeps(clock):
    sleep, sleeps = clock
    calls = []
    report = gate("tiny", deadline=0, snapshot_fn=lambda: calls.append(True), sleep_fn=sleep)
    assert not report["allowed"] and report["reason"] == "elapsed_budget_exhausted"
    assert report["snapshots"] == [] and calls == [] and sleeps == []


def test_deadline_bounds_sleep_and_prevents_another_snapshot(clock):
    sleep, sleeps = clock
    state = healthy()
    state["cpu_percent"] = 100
    calls = []

    def read():
        calls.append(True)
        return state

    report = gate("tiny", deadline=7, snapshot_fn=read, sleep_fn=sleep)
    assert not report["allowed"] and report["reason"] == "elapsed_budget_exhausted"
    assert sleeps == [7] and calls == [True]
    assert report["wait_seconds"] == 7


def test_deadline_elapsed_during_snapshot_cannot_admit(clock):
    sleep, sleeps = clock

    def read():
        sleep(10)
        return healthy()

    report = gate("tiny", deadline=5, snapshot_fn=read, sleep_fn=sleep)
    assert not report["allowed"] and report["reason"] == "elapsed_budget_exhausted"
    assert len(report["snapshots"]) == 1 and report["wait_seconds"] == 0
    assert sleeps == [10]


@pytest.mark.parametrize("deadline,expected_timeout", [(1, 0.45), (20, 3), (0.05, 0.001)])
def test_default_snapshot_timeout_uses_remaining_deadline(deadline, expected_timeout, monkeypatch, clock):
    sleep, sleeps = clock
    calls = []

    def read(base_url, timeout):
        calls.append((base_url, timeout))
        return healthy()

    monkeypatch.setattr("or_agent.resources.snapshot", read)
    report = gate("tiny", deadline=deadline, sleep_fn=sleep)
    assert report["allowed"] and sleeps == []
    assert calls == [("http://localhost:11434", pytest.approx(expected_timeout))]


def test_snapshot_forwards_http_timeout(monkeypatch):
    from io import BytesIO

    monkeypatch.setattr("or_agent.resources._native_snapshot", lambda: {"diagnostics": []})
    calls = []

    def read(url, timeout):
        calls.append(timeout)
        return BytesIO(b'{"models": []}')

    monkeypatch.setattr("or_agent.resources.urlopen", read)
    snapshot(timeout=0.25)
    assert calls == [0.25, 0.25]


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), True])
def test_invalid_snapshot_timeout(timeout):
    with pytest.raises(ValueError, match="timeout"):
        snapshot(timeout=timeout)
