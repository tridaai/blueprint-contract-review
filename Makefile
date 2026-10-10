.PHONY: demo test evals llm-evals serve install install-pip

install:  ## Install the locked set with uv (primary workflow)
	uv sync --extra dev

install-pip:  ## Fallback install: pip with the pinned/frozen set
	pip install -e ".[dev]" -c constraints.txt

demo:  ## Traced demo: one sample contract, end to end (offline)
	uv run contract-agent-demo

test:  ## Run the pytest suite
	uv run pytest -q

evals:  ## Run the golden-dataset evals (gate: outcome accuracy >= 95%)
	uv run python evals/run_evals.py

llm-evals:  ## Opt-in LLM-mode eval pack (needs a provider key; NOT part of the default gate)
	uv run python evals/run_llm_evals.py

serve:  ## Serve the API + web UI on http://localhost:8000
	uv run uvicorn contract_agent.api:app --port 8000
