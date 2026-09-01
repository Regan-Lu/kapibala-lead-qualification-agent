# KapibalaAI Lead Qualification Agent

A small, auditable agent that uses an LLM for lead-intent analysis while enforcing business constraints in deterministic application code.

## Current phase

Phases 0 and 1 are complete:

- runnable FastAPI project skeleton and health endpoint;
- closed enums for the five intents, four allowed actions, and conversation states;
- a Pydantic contract for structured LLM analysis;
- an asynchronous `Analyzer` port and deterministic fake for later state-machine tests.

The LLM output is treated as an untrusted proposal. `proposed_action` is not an executed action: later phases add the deterministic policy, state gates, and explicit action executor that can reject or override it.

`decision_note` is a short observability label, not chain-of-thought and never an authorization signal. It is handled as untrusted display text.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
uvicorn lead_qualification_agent.app:app --reload
```

Open `http://127.0.0.1:8000/health` and expect:

```json
{"status":"ok"}
```

Run tests with:

```bash
pytest
```

Copy `.env.example` to `.env` only when live LLM integration is added. Never commit the real API key.
