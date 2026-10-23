[English](roadmap.md) | [Français](roadmap.fr.md) | [简体中文](roadmap.zh-CN.md)

<a id="or-agent--roadmap-et-checkpoints-de-validation"></a>
# OR-Agent - Roadmap and validation checkpoints



This roadmap traces the milestones and checkpoints achieved. The [README](../README.md) presents the product and its usage; the [technical development log](memory.md) explains the implementation choices.

<a id="m0--cadrage-gap"></a>
## M0 - GAP scope

Objective: define the assignment contract under capacity constraints.

- [x] Fix the mandatory, indivisible, and unique assignment of each task.
- [x] Define costs and consumptions per task-resource pair, capacities, and total cost minimization.
- [x] Set integer bounds and explicit units, with negative costs and zero consumptions.
- [x] Distinguish GAP from routing, precedence, and scheduling.
- [x] Distinguish model validation, fidelity to source, feasibility, and optimality.

Validation: the contract implemented in [models.py](../or_agent/models.py), covered by schema and resolution tests. **Status: completed.**

<a id="m1--validation-et-représentation"></a>
## M1 - Validation and representation

Objective: represent data in strict and controllable models.

- [x] Implement closed models `Gap`, `Extraction`, and `Candidate`.
- [x] Control identifiers, uniqueness, dimensions, numeric domain, and units.
- [x] Fix resources in rows and tasks in columns within matrices.
- [x] Require exact provenance for each field in an extraction `ready`.
- [x] Reject ambiguous JSON, incomplete data, and unknown fields.

Validation: valid and rejected cases covered by [test_models.py](../tests/test_models.py). **Status: completed for structural validation.**

<a id="m2--résolution-cp-sat"></a>
## M2 - CP-SAT Solution

Objective: convert and solve the provided GAP within a time limit.

- [x] Build binary variables, unique assignment, capacities, and integer objective.
- [x] Preserve candidate, objective, bound, version, solver parameters, and solver runtime.
- [x] Distinguish proven optimum, feasible candidate, infeasibility, inconclusive result, and technical error.
- [x] Compare small problems against exhaustive enumeration and test numerical and temporal limits.

Validation: conversion, statuses, and references covered by [test_solver.py](../tests/test_solver.py). **Status: completed for the provided GAP.**

<a id="m3--vérification-indépendante"></a>
## M3 - Independent Verification

Objective: control assignments using a computation independent of the solver.

- [x] Control identifiers, exact task coverage, and duplicates.
- [x] Recalculate integer loads and verify capacities.
- [x] Recalculate cost and compare it to the announced objective.
- [x] Return structured violations and reject malformed candidates.
- [x] Test omissions, duplicates, unknown resources, overloads, and corrupted costs.

Validation: injected corruptions covered by [test_verify.py](../tests/test_verify.py). **Status: completed for feasibility and cost.**

<a id="m4--boucle-agent"></a>
## M4 - Agent Loop

Objective: connect Ollama, validation, and resolution within a bounded loop.

- [x] Deliver the generation CLI and structured diagnostics.
- [x] Implement and compare JSON by prompt, schema-based generation, and bounded correction.
- [x] Limit generation to one call, or two with correction, and apply budgets.
- [x] Preserve already parsed data during correction.
- [x] Log raw responses, errors, observations, and resource controls.
- [x] Test branches with controlled responses and execute a real Ollama pilot via CLI.

Validation: chained workflows covered by [test_agent.py](../tests/test_agent.py). **Status: completed for the CLI loop without HITL.**

<a id="m5--évaluation"></a>
## M5 - Evaluation

Objective: evaluate formulations and final responses based on independent references.

- [x] Prepare the initial corpus of 16 cases: 7 development and 9 test cases.
- [x] Prepare the extended corpus of 130 cases: 30 development and 100 test cases.
- [x] Audit references, fingerprints, and family separations between development and test cases.
- [x] Implement logged campaigns and metrics, counting missing results in denominators.
- [x] Conduct initial, extended, and independent historical evaluations.
- [x] Analyze omissions, erroneous associations, transpositions, and corrections.

Validation: corpora covered by [test_benchmark_data.py](../tests/test_benchmark_data.py) and [test_extended_corpus.py](../tests/test_extended_corpus.py), protocols and metrics by evaluation tests. **Status: exploratory evaluation completed.** Useful decision-making results are summarized in the [technical development log](memory.md#m5--évaluation).

<a id="extensions--routage-déterministe-et-hitl"></a>
## Extensions - Deterministic Routing and HITL

Objective: directly resolve certified formats and submit other formulations to explicit review.

- [x] Extract facts with associations, units, citations, and positions in the source.
- [x] Certify coverage, standard constraints, absence of contradictions, and canonical reconstruction.
- [x] Build and resolve inputs `VERIFIED_COMPLETE` without LLM.
- [x] Produce alternatively a manual draft or an optional Ollama proposal, with unknowns and diagnostics.
- [x] Add correction, validation, review, scope confirmation, approval, and rejection steps.
- [x] Link authorizations to exact source and proposal inputs, then invalidate them after modification.
- [x] Maintain original source blockers after rephrasing the draft.
- [x] Verify guards with simulated approvals.

Validation: reviews, authorizations, and routing covered by [test_hitl.py](../tests/test_hitl.py) and [test_routing.py](../tests/test_routing.py). **Status: completed within the V1 technical scope.**

<a id="extension--interface-web"></a>
## Extension - Web Interface

Objective: consolidate input, review, resolution, and delivery within a local interface.

- [x] Deliver a single-user local server and an interface requiring no frontend build.
- [x] Display steps and diagnostics via SSE events.
- [x] Integrate manual draft, Ollama, edition, validation, review, and approval.
- [x] Persist sessions and consult them after restart without re-triggering processing.
- [x] Add visualizations, SVG/HTML/JSON exports, and invalidation after edition.
- [x] Explicitly allow import of sessions in read-only mode.
- [x] Test HTTP controls, file deletion, and session file protections.
- [x] Execute under Chrome the certified README format, with optimal 6 and verified candidate.
- [x] Execute the HITL manual process, rejection without approval, invalidation, and new approval.
- [x] Verify persistence, diagnostics, visualizations, and consistency of exports.
- [x] Control widths 1440, 1280, and 390 px, with mobile actions and no global overflow or blocking JavaScript error.
- [x] Verify the path without Ollama and a controlled generation response.

Validation: services covered by [test_web.py](../tests/test_web.py), Chrome path by [web_browser.mjs](../tests/web_browser.mjs), with controlled generation and approvals. **Status: completed within the functional and technical web scope V1.**

<a id="clôture-v1"></a>
## V1 Closure

Objective: verify integrated paths and consolidate observed findings.

- [x] Execute a real Ollama pilot from the web with independent references.
- [x] Conduct human reviews and compare initial and approved formulations.
- [x] Control resolution, verification, and exports after investigator correction and human approval.
- [x] Distinguish in the report formulations, review decisions, and solver results.

Validation: observations synthesized in the [technical development log](memory.md#bilan-des-fonctionnalités-et-validations-v1). **Status: paths observed and report completed.**

**V1 - Completed within its functional and technical scope.**
