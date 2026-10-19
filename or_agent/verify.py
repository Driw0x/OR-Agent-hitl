"""Independent arithmetic checker: no solver or converter imports."""

from .models import Candidate, Gap


def verify(problem: Gap, candidate: Candidate | dict) -> dict:
    try:
        candidate = Candidate.model_validate(candidate)
    except ValueError as error:
        return dict(valid=False, violations=[dict(kind="candidate_structure", detail=str(error))],
                    recomputed_objective=None, loads=None)
    violations = []
    counts = {task: 0 for task in problem.tasks}
    loads = {resource: 0 for resource in problem.resources}
    total = 0
    for assignment in candidate.assignments:
        task, resource = assignment.task, assignment.resource
        if task not in counts:
            violations.append(dict(kind="unknown_task", task=task))
        else:
            counts[task] += 1
        if resource not in loads:
            violations.append(dict(kind="unknown_resource", resource=resource))
        if task in counts and resource in loads:
            i, j = problem.resources.index(resource), problem.tasks.index(task)
            loads[resource] += problem.consumption[i][j]
            total += problem.costs[i][j]
    for task, count in counts.items():
        if count != 1:
            violations.append(dict(kind="task_coverage", task=task, count=count, expected=1))
    for resource, capacity in zip(problem.resources, problem.capacities):
        if loads[resource] > capacity:
            violations.append(dict(kind="capacity", resource=resource, load=loads[resource], capacity=capacity))
    if total != candidate.objective:
        violations.append(dict(kind="objective", reported=candidate.objective, recomputed=total))
    return dict(valid=not violations, violations=violations, recomputed_objective=total, loads=loads,
                scope="feasibility for supplied model; no fidelity or optimality proof")
