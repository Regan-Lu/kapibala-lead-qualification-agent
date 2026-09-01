# KapibalaAI Lead Qualification Agent

A small, auditable agent that uses an LLM for lead-intent analysis while enforcing business constraints in deterministic application code.

## Current phase

The repository currently contains the runnable project skeleton and health endpoint. Domain contracts and agent behavior are added in subsequent phases.

## Local setup

```bash
uv sync --extra dev
uv run uvicorn lead_qualification_agent.app:app --reload
```

Open `http://127.0.0.1:8000/health` and expect:

```json
{"status":"ok"}
```

Run tests with:

```bash
uv run pytest
```

Copy `.env.example` to `.env` only when live LLM integration is added. Never commit the real API key.
