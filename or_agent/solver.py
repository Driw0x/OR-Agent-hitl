"""Deterministic GAP conversion and conservative CP-SAT status interpretation."""

import math
import time

import ortools
from ortools.sat.python import cp_model

from .models import Gap


def optimality_evidence(status, objective, bound, absolute_gap=0.0, relative_gap=0.0):
    return (status == "OPTIMAL" and objective is not None and bound is not None
            and absolute_gap == 0 and relative_gap == 0 and objective == bound)


def build_model(problem: Gap):
    model = cp_model.CpModel()
    m, n = len(problem.resources), len(problem.tasks)
    x = [[model.new_bool_var(f"x_{i}_{j}") for j in range(n)] for i in range(m)]
    for j in range(n):
        model.add_exactly_one(x[i][j] for i in range(m))
    for i in range(m):
        model.add(sum(problem.consumption[i][j] * x[i][j] for j in range(n)) <= problem.capacities[i])
    model.minimize(sum(problem.costs[i][j] * x[i][j] for i in range(m) for j in range(n)))
    return model, x


def solve(problem: Gap, time_limit: float = 5.0, *, observe=None):
    if not math.isfinite(time_limit) or not 0 <= time_limit <= 3600:
        raise ValueError("solver time limit must be finite and between 0 and 3600 seconds")
    parameters = dict(max_time_in_seconds=time_limit, num_search_workers=1,
                      random_seed=17, absolute_gap_limit=0.0, relative_gap_limit=0.0)
    result = dict(raw_status=None, outcome="execution_error", candidate=None,
                  objective=None, best_bound=None, solver_proof=False,
                  solver_version=ortools.__version__, parameters=parameters)
    started = time.monotonic()
    stage = 'conversion'
    try:
        if observe:
            observe('conversion', 'running')
        model, x = build_model(problem)
        if observe:
            observe('conversion', 'completed')
        solver = cp_model.CpSolver()
        for name, value in parameters.items():
            setattr(solver.parameters, name, value)
        if observe:
            observe('solver', 'running', seconds=time_limit)
        stage = 'solver'
        status = solver.solve(model)
        if observe:
            observe('solver', 'completed', raw_status=solver.status_name(status), wall_seconds=solver.wall_time)
        result.update(raw_status=solver.status_name(status), outcome="solver",
                      solver_wall_seconds=solver.wall_time, response_stats=solver.response_stats(),
                      best_bound=solver.best_objective_bound)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            result.update(raw_objective=solver.objective_value,
                          raw_best_bound=solver.best_objective_bound)
            assignments = [dict(task=task, resource=resource)
                           for j, task in enumerate(problem.tasks)
                           for i, resource in enumerate(problem.resources) if solver.value(x[i][j])]
            objective = sum(problem.costs[i][j] * solver.value(x[i][j])
                            for i in range(len(problem.resources)) for j in range(len(problem.tasks)))
            # CP-SAT documents this as a bound on the original integer expression.
            # GAP minimizes that expression with offset 0 and scaling factor 1.
            bound = solver.response_proto.inner_objective_lower_bound
            result.update(objective=objective, best_bound=bound,
                          candidate=dict(assignments=assignments, objective=objective))
            result["solver_proof"] = optimality_evidence(result["raw_status"], objective, bound,
                parameters['absolute_gap_limit'], parameters['relative_gap_limit'])
    except Exception as error:
        if observe:
            observe(stage, 'failed', error=f'{type(error).__name__}: {error}')
        result.update(outcome="execution_error", candidate=None, objective=None,
                      solver_proof=False, error=f"{type(error).__name__}: {error}")
    result["elapsed_seconds"] = time.monotonic() - started
    return result
