import itertools
import random
from pathlib import Path

import pytest

from or_agent.models import Gap, read_gap
from or_agent.solver import build_model, optimality_evidence, solve


def enumerate_reference(p):
    candidates = []
    for owners in itertools.product(range(len(p.resources)), repeat=len(p.tasks)):
        loads = [sum(p.consumption[i][j] for j, owner in enumerate(owners) if owner == i)
                 for i in range(len(p.resources))]
        if all(load <= cap for load, cap in zip(loads, p.capacities)):
            candidates.append(sum(p.costs[owner][j] for j, owner in enumerate(owners)))
    return min(candidates) if candidates else None


def test_main_and_infeasible():
    p = read_gap(Path('examples/small.json').read_text())
    result = solve(p)
    assert result['raw_status'] == 'OPTIMAL'
    assert result['objective'] == result['best_bound'] == 6
    assert result['solver_proof']
    data = p.model_dump()
    data['capacities'] = [1, 1]
    result = solve(Gap.model_validate(data))
    assert result['raw_status'] == 'INFEASIBLE'
    assert result['candidate'] is result['objective'] is None


def test_multiple_optima():
    p = Gap(tasks=['A','B'], resources=['X','Y'], costs=[[1,1],[1,1]],
            consumption=[[1,1],[1,1]], capacities=[1,1], cost_unit='credit',resource_unit='slot')
    result = solve(p)
    assert result['objective'] == 2
    assert len({a['resource'] for a in result['candidate']['assignments']}) == 2


def test_exhaustive_random_small_instances():
    rng = random.Random(17)
    for _ in range(30):
        m, n = rng.randint(1,3), rng.randint(1,5)
        p = Gap(tasks=[f'T{j}' for j in range(n)],resources=[f'R{i}' for i in range(m)],
                costs=[[rng.randint(-3,8) for _ in range(n)] for _ in range(m)],
                consumption=[[rng.randint(0,4) for _ in range(n)] for _ in range(m)],
                capacities=[rng.randint(0,8) for _ in range(m)],cost_unit='credit',resource_unit='slot')
        expected = enumerate_reference(p)
        result = solve(p)
        assert result['objective'] == expected
        assert result['raw_status'] == ('INFEASIBLE' if expected is None else 'OPTIMAL')


@pytest.mark.parametrize('status', ['FEASIBLE','INFEASIBLE','MODEL_INVALID','UNKNOWN',None])
def test_simulated_status_policy(status):
    assert not optimality_evidence(status, 6, 6)


def test_gap_policy_simulated():
    assert not optimality_evidence('OPTIMAL', 6, 6, relative_gap=0.01)
    assert not optimality_evidence('OPTIMAL', 6, 5)
    assert not optimality_evidence('OPTIMAL', None, None)


def test_actual_zero_budget_unknown():
    p = read_gap(Path('examples/small.json').read_text())
    result = solve(p, 0)
    assert result['raw_status'] == 'UNKNOWN'
    assert result['candidate'] is None


def test_execution_error_and_model_invalid(monkeypatch):
    import or_agent.solver as module
    from ortools.sat.python import cp_model
    p = read_gap(Path('examples/small.json').read_text())
    def broken(_):
        raise RuntimeError('injected execution failure')
    monkeypatch.setattr(module, 'build_model', broken)
    assert solve(p)['outcome'] == 'execution_error'
    model = cp_model.CpModel()
    model.new_int_var(-(2**63), 2**63-1, 'invalid')
    monkeypatch.setattr(module, 'build_model', lambda _: (model, []))
    result = solve(p)
    assert result['raw_status'] == 'MODEL_INVALID'
    assert result['candidate'] is None


@pytest.mark.parametrize('limit', [-1,float('nan'),float('inf')])
def test_invalid_limit(limit):
    with pytest.raises(ValueError):
        solve(read_gap(Path('examples/small.json').read_text()),limit)


@pytest.mark.parametrize('costs', [[[14, 21], [23, 12]], [[-14, -21], [-23, -12]],
                                 [[0, 0], [0, 0]], [[1, 1], [1, 1]],
                                 [[-10**9, 10**9], [10**9, -10**9]]])
def test_integer_objective_and_native_bound(costs):
    from or_agent.verify import verify
    p = Gap(tasks=['T1', 'T2'], resources=['R1', 'R2'], costs=costs,
            consumption=[[2, 3], [3, 2]], capacities=[3, 3],
            cost_unit='credit', resource_unit='slot')
    model, _ = build_model(p)
    assert model.proto.has_objective() and not model.proto.has_floating_point_objective()
    assert model.proto.objective.offset == 0 and model.proto.objective.scaling_factor == 1
    result = solve(p)
    assert result['outcome'] == 'solver'
    assert type(result['objective']) is type(result['best_bound']) is int
    assert result['objective'] == result['best_bound'] == enumerate_reference(p)
    assert verify(p, result['candidate'])['valid'] and result['solver_proof']
    if costs == [[14, 21], [23, 12]]:
        assert result['raw_objective'] == result['raw_best_bound'] == 26.000000000000004


@pytest.mark.parametrize('cost', [-10**9, 10**9])
def test_full_pair_limit_integer_sum(cost):
    from or_agent.verify import verify
    p = Gap(tasks=[f'T{j}' for j in range(10000)], resources=['R'], costs=[[cost]*10000],
            consumption=[[0]*10000], capacities=[0], cost_unit='credit', resource_unit='slot')
    result = solve(p)
    assert result['objective'] == result['best_bound'] == cost * 10000
    assert result['solver_proof'] and verify(p, result['candidate'])['valid']


@pytest.mark.parametrize('status,bound,proof', [('OPTIMAL', 26, True), ('OPTIMAL', 25, False),
                                              ('FEASIBLE', 26, False), ('FEASIBLE', 25, False)])
def test_native_integer_bound_status_policy(monkeypatch, status, bound, proof):
    from types import SimpleNamespace
    from ortools.sat.python import cp_model
    import or_agent.solver as module
    p = Gap(tasks=['T1', 'T2'], resources=['R1', 'R2'], costs=[[14, 21], [23, 12]],
            consumption=[[2, 3], [3, 2]], capacities=[3, 3], cost_unit='credit', resource_unit='slot')

    class ControlledSolver:
        parameters = SimpleNamespace()
        objective_value = best_objective_bound = 26.000000000000004
        response_proto = SimpleNamespace(inner_objective_lower_bound=bound)
        wall_time = 0

        def solve(self, model): return getattr(cp_model, status)
        def status_name(self, status): return status.name
        def response_stats(self): return 'controlled status; not a real CP-SAT run'
        def value(self, variable): return int(variable.index in (0, 3))

    monkeypatch.setattr(cp_model, 'CpSolver', ControlledSolver)
    result = module.solve(p)
    assert result['candidate']['objective'] == result['objective'] == 26
    assert result['best_bound'] == bound and result['solver_proof'] is proof
    assert result['raw_status'] == status


@pytest.mark.parametrize('gaps', [dict(absolute_gap=0.5), dict(relative_gap=0.01)])
def test_nonzero_tolerances_never_establish_exact_proof(gaps):
    assert not optimality_evidence('OPTIMAL', 26, 26, **gaps)
