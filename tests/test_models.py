import copy
from pathlib import Path

import pytest

from or_agent.models import Extraction, Gap, load_json, read_gap


def sample():
    return load_json(Path("examples/small.json").read_text())


def test_valid_roundtrip():
    assert read_gap(Path("examples/small.json").read_text()).model_dump() == sample()


@pytest.mark.parametrize("field,value", [
    ("costs", [[1], [2]]), ("capacities", [1]),
    ("tasks", ["T1", "T1", "T3"]), ("resources", ["bad id", "R2"]),
    ("capacities", [-1, 2]), ("capacities", [True, 2]),
    ("capacities", [4.0, 2]), ("capacities", ["4", 2]),
    ("capacities", [10**9 + 1, 2]), ("cost_unit", " "),
    ("deadline", 4), ("consumption", [[2, -1, 2], [2, 2, 2]]),
])
def test_invalid(field, value):
    data = sample()
    data[field] = value
    with pytest.raises(ValueError):
        Gap.model_validate(data)


def test_missing_and_extra_constraint():
    data = sample()
    del data["capacities"]
    with pytest.raises(ValueError, match="capacities"):
        Gap.model_validate(data)


@pytest.mark.parametrize("text", ['{', '{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}'])
def test_json_boundary(text):
    with pytest.raises(ValueError):
        load_json(text)


@pytest.mark.parametrize("disposition", ["clarify", "unsupported", "invalid"])
def test_nonready(disposition):
    extraction = Extraction(disposition=disposition, problem=None, issues=["explain missing data"], provenance={})
    assert extraction.problem is None
    with pytest.raises(ValueError):
        Extraction(disposition=disposition, problem=Gap.model_validate(sample()), issues=["reason"], provenance={})


def test_provenance_is_not_fidelity():
    data = sample()
    source = "This quote exists but does not prove the extracted numbers."
    x = Extraction(disposition="ready", problem=Gap.model_validate(data), issues=[],
                   provenance={k: source for k in data})
    x.check_source(source)
    with pytest.raises(ValueError, match="quote"):
        x.check_source("different source")


def test_numeric_edge_and_size():
    data = sample()
    data.update(costs=[[-10**9, 0, 10**9], [0, 0, 0]], consumption=[[0]*3]*2, capacities=[0,0])
    assert Gap.model_validate(data).costs[0][0] == -10**9
    data = copy.deepcopy(data)
    data["tasks"] = [f"T{i}" for i in range(5001)]
    with pytest.raises(ValueError, match="10000"):
        Gap.model_validate(data)
