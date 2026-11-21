# Local development without Docker

This pass was built and verified entirely without Docker (and without
Harbor, the gated dataset, or any live model call). Here is how to work on
the harness the same way.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt          # websockets==15.0.1
.venv/bin/pip install matplotlib                    # lab figures only (optional)
```

`node_modules/` is NOT needed for offline development — the Node side
(Pi 1.1.0) is only exercised by the real evaluator; every offline test fakes
the `pi` subprocess with small scripted stand-ins (`tests/test_orchestrator.py`,
`tests/test_e2e_loopback.py`).

## Running the tests

```bash
.venv/bin/python -m unittest discover -s tests -v   # whole suite (~10 s)
.venv/bin/python -m unittest tests.test_solvers     # one module
```

186 tests, all offline: unit tests over the protocol/session/relay/MCP
clients, model-configuration conventions, the contract/authority/evidence
modules, the solver library (hand-checked values), the orchestrator (with a
role-aware fake pi over a real loopback relay), and two end-to-end loopback
flows with a mock evaluator. No Docker, no network beyond loopback, no
credentials, no task data.

## What you can verify locally

- Protocol semantics and limits (`tests/test_protocol.py`).
- Provider/env conventions for all three routes (`test_model_configuration.py`).
- Deliverable-contract parsing/validation, authority resolution, evidence
  ledger, context helpers (`test_contract.py`, `test_authority.py`,
  `test_evidence.py`, `test_context.py`).
- Solver math against hand-checked numbers (`test_solvers.py`).
- Orchestration logic and loopback behavior (`test_orchestrator.py`,
  `test_e2e_loopback.py`).

## What you CANNOT verify locally (by design)

- `python -m kit check` and `eval/run.py` need the competition repository,
  Docker, and Harbor — deferred to the Docker machine.
- Real model behavior: the fake `pi` scripts emit scripted events; they
  prove plumbing, not model quality.
- Any claim about scores: nothing here measures one.

## The offline lab

`kaggle/ple_offline_lab.py` (and the notebook) run the same checks plus
figures and the writeup draft — CPU-only, no network:

```bash
.venv/bin/python kaggle/ple_offline_lab.py
```

## Discipline reminders

- Never commit `.env` or keys (the repo is public; also see §6 of AGENTS.md).
- Never modify `artifacts/` — it is immutable baseline evidence.
- Keep new tests offline and deterministic: fake subprocesses, in-memory
  containers, loopback sockets only.
