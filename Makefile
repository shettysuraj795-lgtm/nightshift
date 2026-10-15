PROMPT ?= triage_v1

.PHONY: infra-up infra-down api ui data ingest test eval

infra-up:  ## Start Qdrant and Phoenix in Docker
	docker compose up -d
	@echo "Qdrant:  http://localhost:6333/dashboard"
	@echo "Phoenix: http://localhost:6006"

infra-down:  ## Stop Qdrant and Phoenix (data volumes are kept)
	docker compose down

# No access log: it would record caller IP addresses (no operator identity in logs).
# Reload only on API code changes; a restart resets the in-memory chaos flags.
api:  ## Run the FastAPI server on :8000
	uv run uvicorn app.main:app --reload --reload-dir app --port 8000 --no-access-log

ui:  ## Run the Streamlit UI on :8501
	PYTHONPATH=. uv run streamlit run ui/streamlit_app.py --server.port 8501

data:  ## Regenerate synthetic data from data/registry.yaml (seed 42)
	PYTHONPATH=. uv run python scripts/generate_data.py --seed 42

ingest:  ## Embed the KB and tickets into Qdrant (needs make infra-up)
	PYTHONPATH=. uv run python scripts/ingest.py

test:  ## Run the unit tests
	uv run pytest

eval:  ## Run the eval suite and regression gate (PROMPT=triage_v2_regression to test the gate)
	PYTHONPATH=. uv run python evals/run_evals.py --prompt $(PROMPT)
