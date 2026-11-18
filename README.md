[English](README.md) | [Français](README.fr.md) | [简体中文](README.zh-CN.md)

<a id="or-agent"></a>
# OR-Agent



Local application for structuring, solving, and verifying assignment problems under capacity constraints.

<a id="présentation"></a>
## Presentation

OR-Agent transforms data from a **Generalized Assignment Problem (GAP)** into a structured formulation, validates this formulation, and then solves it using CP-SAT. An independent verifier recomputes the assignments, loads, and costs.

The web interface and CLI offer deterministic extraction, optional local generation using Ollama, and a human-in-the-loop (HITL) review and approval process. Sessions, visualizations, and exports allow for consultation and preservation of results.

**Version 1 completed within its functional and technical scope.**

<a id="problème-gap-pris-en-charge"></a>
## GAP problem handled

The GAP addressed is based on the following rules:

- All tasks are mandatory and indivisible.
- Each task is assigned exactly once to a resource.
- The consumption of a task may depend on the chosen resource.
- For each resource, the sum of consumptions does not exceed its capacity.
- The objective is to minimize the sum of assignment costs.

Costs are integers between `-10^9` and `10^9`. Consumptions and capacities are integers between `0` and `10^9`. Negative costs and zero consumptions are allowed. Units must be explicitly specified; no implicit conversions are performed. The model accepts at most **10,000 task-resource pairs**.

Routing, precedence constraints, deadlines, temporal scheduling, optional tasks, and additional task constraints fall outside the scope of this contract. An incomplete request triggers clarification; a constraint outside the scope must not be removed to make the problem solvable.

<a id="fonctionnement"></a>
## Operation

```text
Original statement
    |
    v
Deterministic fact extraction and scope checks
    |                                          |
    v                                          v
Certified closed format                 Uncertified input
Direct GAP construction                 Manual or Ollama draft
No LLM or human review                  Correction and validation
    |                                   Review and scope confirmation
    |                                   Approval of the exact revision
    |                                          |
    +----------------------+-------------------+
                           |
                           v
                    CP-SAT conversion
                           |
                           v
                    Solving within budget
                           |
                           v
                    Independent arithmetic verification
                           |
                           v
                    Result, visualizations and exports
```

Deterministic certification applies only to recognized closed formats, with data and provenance controls. For all other inputs, manual or Ollama-generated proposals must be controlled, reviewed, and approved. Approval authorizes the reviewed revision; it does not automatically guarantee fidelity to the original statement. Any modification invalidates associated permissions.

A validated model respects the data contract. A verified candidate is feasible for the provided GAP. Optimality requires, in addition, a CP-SAT proof:

- `OPTIMAL`: optimum established when the proof and candidate verification are valid.
- `FEASIBLE`: candidate available, submitted to the verifier; optimality not proven.
- `INFEASIBLE`: provided GAP proven infeasible.
- `UNKNOWN`: search inconclusive within the budget.

A technical error is reported separately.

<a id="fonctionnalités"></a>
## Features

<a id="extraction-et-formulation"></a>
### Extraction and Formulation

- Strict Pydantic models: types, dimensions, unique identifiers, units, and rejection of unknown fields.
- Data provenance and diagnostics on missing, contradictory, or uninterpretable elements.
- Deterministic reconstruction for recognized closed formats.
- Editing a readable proposal, with validation of task-resource associations and reconstructed matrices.
- Human review, confirmation of the GAP scope, and approval linked to revision fingerprints.

<a id="résolution-et-vérification"></a>
### Resolution and verification

- Binary assignment variables, cumulative capacities, and cost objective constructed by the code.
- CP-SAT resolution with time limit, candidate and bound available according to status.
- Independent verification of task coverage, load, and total cost.
- Presentation distinguishing feasible solution, proven optimum, infeasibility, and absence of conclusion.

<a id="interface-et-traçabilité"></a>
### Interface and traceability

- Local web interface, without frontend build, with step tracking via server events (SSE).
- Persistent sessions, consultation after restart, and generation diagnostics.
- Visualization of assignments and utilized capacities; exports in SVG, HTML, and JSON formats.
- Read-only import of existing sessions explicitly authorized at startup.

Evaluation tools audit corpora and compare generation and correction methods.

<a id="architecture-et-technologies"></a>
## Architecture and technologies

```text
or_agent/                      Project core: extraction, validation, human review, solving and web interface
data/                          Datasets and evaluation references
examples/                      GAP problem examples
tests/                         Automated tests
docs/                          Roadmap and technical development log
```

The core uses **Python**, **Pydantic**, **OR-Tools CP-SAT**, and **pytest**. Ollama provides optional local generation with `qwen2.5-coder:7b`. The HTTP server and Ollama calls use the standard Python library; the frontend uses HTML, CSS, and JavaScript.

<a id="installation"></a>
## Installation

Prerequisites: **Python 3.12 minimum; verified installations on Python 3.12 and 3.13**, Git; Ollama optional. The reproducible environment versions are fixed in [requirements.txt](requirements.txt). Package dependencies and the same minimal Python version are declared in [pyproject.toml](pyproject.toml).

Under Windows PowerShell, clone the repository. For an existing copy, start environment creation from its root.

```powershell
git clone https://github.com/Driw0x/OR-Agent-hitl.git
cd OR-Agent-hitl
python -m venv .venv
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install -r requirements.txt
& $py -m pip install --no-deps -e .
& $py -m pip check
```

Under Linux:

```sh
git clone https://github.com/Driw0x/OR-Agent-hitl.git
cd OR-Agent-hitl
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m pip check
```

`python3` must correspond to Python 3.12 minimum. If it points to an older version, explicitly use an installed compatible interpreter, for example `python3.12 -m venv .venv`. The fixed dependencies also require Python 3.12 minimum.

Editable installation mode also provides the command `or-agent`. The examples below use `python -m or_agent` to explicitly identify the Python environment.

<a id="utilisation-de-linterface-web"></a>
## Using the web interface

Start the interface from the root, under PowerShell:

```powershell
$py = ".\.venv\Scripts\python.exe"
$env:PYTHONIOENCODING = "utf-8"
& $py -B -m or_agent web
```

Under Linux: `.venv/bin/python -B -m or_agent web`.

Open [http://127.0.0.1:8765](http://127.0.0.1:8765), with this exact address rather than `localhost`. The server is local and single-user. Sessions persist in `.or-agent-web/`. **Ctrl+C** waits for active processing to complete before shutdown, without terminating a shared Ollama server.

1. Enter a GAP problem or select a built-in example, then click **Lancer l'analyse** (Run analysis).
2. Review extracted facts and diagnostics. A fully certified input is resolved directly without LLM or human review.
3. Otherwise, enter a proposal or click **Demander une proposition Ollama** (Request an Ollama proposal). Compare it with the source, make corrections, click **Enregistrer les modifications** (Save changes), then **Vérifier la proposition** (Verify proposal).
4. Click **Effectuer la revue** (Perform review), then **Confirmer la portée** (Confirm scope). Check the approval box and click **Résoudre le GAP approuvé** (Solve approved GAP) to approve and solve this exact revision.
5. Check the status, cost, resource loads, and independent verification.
6. Examine the visualizations and export the results. Any modification invalidates approvals and current exports.

Missing data, contradictions, and out-of-scope constraints block approval. A source contradiction requires a corrected statement in a new session: clearing the diagnosis from the draft does not resolve it. The manual workflow and certified examples work without Ollama.

The options `--port`, `--sessions`, `--base-url`, and `--import-session` allow adapting the launch. Consult `& $py -m or_agent web --help`; under Linux, replace `& $py` with `.venv/bin/python`.

<a id="exemple-sans-llm"></a>
### Example without LLM

This statement follows the closed-line-labeled format. It can be pasted into the interface:

```text
Assign each task exactly once; minimize total cost within capacities.
Tasks: T1, T2, T3.
Resources: R1, R2.
Cost unit: credit.
Resource unit: slot.
R1 capacity: 4.
R1 costs: T1=2, T2=8, T3=3.
R1 consumption: T1=2, T2=2, T3=2.
R2 capacity: 2.
R2 costs: T1=5, T2=1, T3=4.
R2 consumption: T1=2, T2=2, T3=2.
```

The optimal assignment is `T1 → R1`, `T2 → R2`, `T3 → R1`. The cost is **6 credits**, with a load of **4 slots on R1** and **2 slots on R2**. The same instance is provided in JSON in [examples/small.json](examples/small.json).

Keywords, punctuation, and explicit associations belong to the recognized format. A free translation or rephrasing may trigger human review. [examples/small.txt](examples/small.txt) is a prose version intended for the generation workflow; it is not the certified format above.

<a id="utilisation-du-cli"></a>
## CLI Usage

<a id="valider-résoudre-et-vérifier-un-gap-json"></a>
### Validate, resolve, and verify a GAP JSON

```powershell
$py = ".\.venv\Scripts\python.exe"
& $py -B -m or_agent validate examples/small.json
& $py -B -m or_agent solve examples/small.json --seconds 5
& $py -B -m or_agent verify examples/small.json examples/candidate.json
```

JSON matrices use **resources in rows and tasks in columns**. `validate` controls the structural contract. `solve` resolves this model and verifies its candidate. `verify` recalculates feasibility and cost of a provided candidate, without seeking an optimum. These commands work directly on the GAP JSON.

[examples/infeasible.json](examples/infeasible.json) illustrates an infeasible instance; [examples/multiple.json](examples/multiple.json) contains an instance with multiple optima. `schema` exposes the expected output schema for extraction.

<a id="analyser-un-énoncé-avant-admission"></a>
### Analyze a statement before admission

```powershell
& $py -B -m or_agent route analyze examples/small.txt
```

This command displays deterministic facts and the certificate, without calling Ollama or resolving. `route submit` then creates a session with certified branch or HITL. The sub-commands `review`, `validate`, `scope`, `approve`, `reject`, `status`, and `solve` expose the review steps. Consult `& $py -m or_agent route --help` and the [technical development log](docs/memory.md) for review contracts.

<a id="génération-et-correction-bornée"></a>
### Bounded generation and correction

```powershell
& $py -B -m or_agent agent examples/small.txt --total-seconds 180 --solver-seconds 5
```

Structured generation allows at most one correction after diagnosis, i.e., a maximum of two calls. `--max-calls 1` limits processing to a single call. The command `agent` resolves a validated extraction directly, without human approval. To control the proposal before resolution, use the web interface or `route`.

<a id="configuration-ollama-facultative"></a>
## Optional Ollama configuration

Ollama is optional. After its separate installation, prepare the model:

```powershell
ollama pull qwen2.5-coder:7b
ollama list
```

The web uses `http://127.0.0.1:11434` by default; `--base-url` allows selecting another address. It verifies the expected model digest in [web.py](or_agent/web.py): a model with the same name but a different digest may be rejected. The CLI `agent` also accepts `--model`; this does not change the model authorized by the web.

Resource controls may reject generation or interrupt client-side waiting. A client timeout does not guarantee cancellation of computation in Ollama. The commands `resources` and `ollama` expose available diagnostics; manual input remains usable if generation is unavailable.

Windows resource monitoring works without configuration. Native Linux hosts can enable the equivalent checks with `OR_AGENT_LINUX_NATIVE=1` when OR-Agent and Ollama share the same host limits:

```bash
OR_AGENT_LINUX_NATIVE=1 .venv/bin/python -B -m or_agent web
```

Linux admission reads RAM, CPU and overcommit policy from `/proc`. Strict commit capacity is enforced when `vm.overcommit_memory=2`; modes `0` and `1` use the measured available-RAM check. Containers and cgroup limits are not supported by this switch.

<a id="tests"></a>
## Tests

From the root, without launching an Ollama campaign:

```powershell
$py = ".\.venv\Scripts\python.exe"
& $py -B -m pytest -q -ra
& $py -B -m or_agent.evaluate audit
& $py -B -m or_agent.extended_corpus audit
```

On Linux, replace `& $py` with `.venv/bin/python`.

Tests cover schemas, CP-SAT vs exhaustive enumeration, corrupted candidates, budgets, provenance, evaluations, and HITL/web sessions. Generation tests use controlled responses.

The full suite requires **Node 22 minimum and Chrome/Chromium** to control a real browser via its debugging protocol, without additional JavaScript dependencies. `OR_AGENT_CHROME` allows providing the browser path if not detected. The absence of tools results in an explicit failure. File protection tests require link creation permissions.

To run only browser paths, deterministic and HITL workflows, with control over exports and widths 1440, 1280, and 390 px:

```powershell
& $py -B -m pytest -q -s tests/test_web.py::test_browser_session_deletion_and_combined_approval
```

The browser uses the local HTTP server and the real CP-SAT solver. Ollama unavailability and generation responses are controlled; no actual LLM generation is required.

Corpora and their references enable offline audits without Ollama. The [V1 Summary](docs/memory.md#bilan-des-fonctionnalités-et-validations-v1) summarizes the technical validations performed.

<a id="documentation"></a>
## Documentation

- [Roadmap](docs/roadmap.md): milestones, achieved checkpoints, and V1 status.
- [Technical Development Log](docs/memory.md): achievements from M0–M5, HITL, deterministic routing, web interface, and V1 validations.
- [Examples](examples/small.json): starting point to verify resolution without LLM.

<a id="licence"></a>
### License

This project is distributed under the MIT License. See [LICENSE](LICENSE).

Copyright © 2026 Victor YE.
