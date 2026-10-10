# Contributing

This is a reference prototype. Contributions that make the core clearer,
better tested, or easier to take and adapt are welcome.

## Dev setup

Primary workflow is [uv](https://docs.astral.sh/uv/):

```bash
uv sync --extra dev
```

No uv? `python3 -m venv .venv && source .venv/bin/activate`, then
`pip install -e ".[dev]" -c constraints.txt`.

## Before opening a PR

Run all three and make sure they pass:

```bash
uv run pytest -q                  # full test suite
uv run python evals/run_evals.py  # golden-dataset eval gate
uv run contract-agent-demo        # end-to-end demo trace
```

(`make test`, `make evals`, and `make demo` wrap the same commands.)

## Code style

- Python 3.10–3.12, typed with Pydantic schemas at the boundaries.
- Deterministic facts are computed with code, never with a model.
- Guardrails are code, not prompts — a rule that matters lives in
  `src/contract_agent/guardrails.py` with a test.
- Keep the frontend to one static HTML file, inline CSS/vanilla JS,
  no dependencies, no build step.

## Two hard rules

- **Synthetic data only.** Never commit a real contract, real
  counterparty terms, or privileged legal material — not in samples,
  tests, evals, or issue reports.
- **Honest claims only.** No invented clients, results, or accuracy
  figures. Eval numbers describe the synthetic golden set and are a
  regression gate, not a real-world benchmark — word them that way.
