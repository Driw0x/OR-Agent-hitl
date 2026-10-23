[English](memory.md) | [Français](memory.fr.md) | [简体中文](memory.zh-CN.md)

<a id="or-agent--mémoire-technique-du-projet"></a>
# OR-Agent - Technical development log



This technical development log traces the achievements and technical decisions of OR-Agent. The [README](../README.md) presents the product and commands; the [roadmap](roadmap.md) preserves the achieved checkpoints.

<a id="m0--cadrage-gap"></a>
## M0 - GAP scope

The project retains the mandatory and indivisible assignment of each task to a resource. For a resource `i` and a task `j`, `x[i,j]` is a binary variable, `c[i,j]` the cost, `a[i,j]` the consumption, and `b[i]` the capacity:

```text
Minimize: sum(i,j) c[i,j] * x[i,j]
Subject to:
    for each task j: sum(i) x[i,j] = 1
    for each resource i: sum(j) a[i,j] * x[i,j] <= b[i]
    x[i,j] belongs to {0, 1}
```

This complete contract enables the construction and control of the model without additional interpretation within the solver. The [GAP scope](../README.md#problème-gap-pris-en-charge) defines the bounds, units, and admissible constraints. Model validation, fidelity to the source, feasibility, and optimality are distinct controls.

<a id="m1--validation-et-représentation"></a>
## M1 - Validation and representation

[models.py](../or_agent/models.py) provides the common contracts for the CLI, the agent, and the solver: `Gap` for data, `Extraction` for the arrangements `ready`, `clarify`, `unsupported` and `invalid`, and `Candidate` for assignments and the announced cost.

Pydantic enforces strict and closed models. Resources occupy the rows of matrices and tasks the columns. Controls pertain to unique identifiers, dimensions, numeric domains, and units; the JSON reader also rejects duplicated keys and non-finite numbers.

An extraction `ready` requires a complete problem, no blocking diagnosis, and an exact citation from the source for each of the seven GAP fields. Other arrangements require an explanation and do not contain a problem to solve. Valid cases and rejections are covered by [test_models.py](../tests/test_models.py).

<a id="m2--résolution-cp-sat"></a>
## M2 - CP-SAT Resolution

[solver.py](../or_agent/solver.py) constructs binary variables, unique assignments, capacities, and the objective. The LLM provides data, never executable code to the solver.

CP-SAT uses a worker, a fixed seed, and zero tolerance for optimality. The default budget is five seconds. The result preserves candidate, objective, best bound, version, parameters, and solver time. The candidate cost is recalculated in integer arithmetic; proof of optimality requires `OPTIMAL` and equality between the objective and the bound.

[test_solver.py](../tests/test_solver.py) compares small cases against exhaustive enumeration and checks status, numerical limits, and budgets. The output associates the CP-SAT proof with independent candidate verification.

<a id="m3--vérification-indépendante"></a>
## M3 - Independent Verification

[verify.py](../or_agent/verify.py) uses a separate arithmetic computation, without importing the solver or the converter. It controls identifiers, exact task coverage, loads, capacities, and total cost. Violations are returned in a structured format.

[test_verify.py](../tests/test_verify.py) injects omissions, duplicates, unknown identifiers, overloads, and erroneous costs. On the [provided example](../examples/small.json), the verifier recovers the loads `R1=4`, `R2=2` and the cost `6`; CP-SAT independently establishes optimality.

<a id="m4--boucle-agent"></a>
## M4 - Agent loop

[agent.py](../or_agent/agent.py) connects to Ollama, decoding, validation, resolution, and delivery. Generation has evolved from the JSON requested by instruction to a structured schema with bounded correction. Evaluation tools preserve strategies `json`, `structured`, and `structured-repair`.

The CLI `agent` uses structured generation with at most one correction, i.e., two calls maximum. Corrections preserve already parsed data. Budgets limit calls, total duration, and resolution; observations record raw responses, errors, diagnostics, and resource checks.

Branches are covered by [test_agent.py](../tests/test_agent.py) with controlled responses, and a real Ollama pilot has been executed in CLI. This path directly resolves a validated extraction; human oversight is provided by `route` and the web.

<a id="m5--évaluation"></a>
## M5 - Evaluation

The [initial corpus](../data/benchmark.json) contains **16 cases: 7 development and 9 test**. The [extended corpus](../data/extended-benchmark.json) contains **130 cases: 30 development and 100 test**. They cover feasible and infeasible instances, negative costs, zero consumption, ambiguities, and out-of-scope requests.

Evaluation tools separate preparation, execution, and re-reading of measures. Audits control footprints, references, and family separations. For the **105 complete instances** in the extended corpus, they verify references, CP-SAT resolution, and reconstruction from source. Missing results remain counted in the denominators.

Historical evaluations have identified omissions, erroneous associations, and transpositions, without any final correct response recovered through automatic repair. The web pilot produced six incomplete formulations and one fully erroneous formulation out of seven complete GAP sources, i.e., **0/7 complete and faithful formulations**. These observations motivate the explicit review described below, without constituting a general reliability measure.

<a id="pourquoi-le-hitl-a-été-introduit"></a>
## Why HITL was introduced

Structured generation alone is insufficient to guarantee fidelity to the original statement. OR-Agent therefore employs explicit human review before authorizing any HITL path resolution. The reviewer compares the source, data, and constraints; their approval authorizes the examined revision.

<a id="implémentation-du-hitl"></a>
## HITL Implementation

[hitl.py](../or_agent/hitl.py) defines the schema `HITL-GAP: 1`: objective, inventories, units, capacities, and a record per task-resource pair. An unknown value is represented by `?` and blocks completeness; lines `Issue:` contain diagnostics.

The path preserves the original request and proceeds with:

1. Manual proposal or Ollama, followed by correction of the draft.
2. Syntax validation, coverage, and data validation.
3. Review of the source, the proposal, and reconstructed matrices.
4. Confirmation of the GAP scope when the path requests it.
5. Explicit approval of the reviewed revision.
6. Resolution of the approved GAP and verification of the candidate.

Sessions preserve `original.txt`, `proposal.txt`, `session.json`, diagnostics, and events. Approval links the source and proposal fingerprints; approved elements are stored under `approved/`. A modification invalidates the review, confirmation, and approval.

The CLI uses confirmations `SCOPE:<review-token>` and `APPROVE:<review-token>`. The LLM cannot grant these authorizations. [test_hitl.py](../tests/test_hitl.py) verifies corrections, rejections, unknowns, and resolution refusals with missing or expired authorization.

<a id="extraction-et-routage-déterministes"></a>
## Deterministic extraction and routing

[deterministic.py](../or_agent/deterministic.py) extracts facts associated with a field, a task, a resource, a value, a unit, a citation, and positions in the source. Positions follow Python character indices, with interval `[start, end)`.

[routing.py](../or_agent/routing.py) reconstructs the GAP and controls inventories, units, numerical and textual coverage, standard constraints, absence of contradictions, and canonical consistency. Certificate `VERIFIED_COMPLETE` authorizes branch `AUTONOMOUS`, without LLM or review, only for recognized closed formats. Other certificates route to human oversight or signal a blocker.

The manual draft reiterates individually verified facts and leaves missing data at `?`. Unrecognized prose requires explicit scope confirmation. Contradictions and exclusions detected in the source persist after redrafting the manual draft; correcting the source requires a new session. [test_routing.py](../tests/test_routing.py) covers certificates, reconstruction, and admission.

<a id="interface-web"></a>
## Web interface

[web.py](../or_agent/web.py) reuses routing, HITL, resolution, and verification services. The HTML/CSS/JavaScript interface presents source, facts, diagnostics, formulation, and results without frontend build. SSE events follow the processing steps.

Sessions persist in `.or-agent-web/` and remain accessible after restart without re-executing calculations. Visualizations show assignments and capacities; SVG, HTML, and JSON exports are tied to the current revision. Imported sessions are authorized at startup and accessed in read-only mode.

The single-user local server controls the origin of mutations and access to session files. Optional generation verifies the Ollama model digest and available resources; the manual draft remains accessible without Ollama.

[test_web.py](../tests/test_web.py) covers HTTP services, authorizations, file deletion, and file protection. [web_browser.mjs](../tests/web_browser.mjs) executes deterministic and HITL workflows in Chrome, with real CP-SAT and verifier, controlled generation, and approvals.

<a id="bilan-des-fonctionnalités-et-validations-v1"></a>
## Feature overview and V1 validations

The project integrates a strict GAP contract, deterministic extraction and routing, optional local generation, HITL review, CP-SAT resolution, independent verification, CLI, web sessions, visualizations, and exports.

Technical validations include: **864 successful tests, zero failures, and zero ignored tests** during final validation under Windows.

- The V1 unites strict GAP contract, deterministic extraction and routing, optional local generation, HITL review, CP-SAT resolution, independent verification, CLI, web sessions, visualizations, and exports.
- Deterministic Chrome trajectory with HITL: authorizations, invalidation after editing, persistence, visualizations and consistency of JSON/SVG/HTML exports at widths **1440, 1280 and 390 px**, with mobile actions.
- Installations on Windows under Python 3.13 and Linux Ubuntu WSL2 under Python 3.12, with fixed dependencies, editable installation, examples, and offline audits. Chrome trajectories have been validated on Windows.
- Imports, compilation of sources, consistency of dependencies, CLI commands, and audits of both corpora.

Historical controls after reconstruction of proposals by the investigator and human approval have yielded six optima **15, 2, −7, 5, 55 and 56**, with verified candidates, and feasibility consistent with enumeration. The 21 associated export JSON/SVG/HTML controls have succeeded. The three distinct deterministic controls have produced the optima **6 and −5**, verified, and feasibility consistent.

**V1 completed within its functional and technical scope.**
