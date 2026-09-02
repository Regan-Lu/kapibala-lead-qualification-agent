# KapibalaAI Lead Qualification Agent

A small, auditable agent that uses an LLM for lead-intent analysis while enforcing business constraints in deterministic application code.

## Current phase

Phases 0, 1, and 2 are complete:

- runnable FastAPI project skeleton and health endpoint;
- closed enums for the five intents, four allowed actions, and conversation states;
- a Pydantic contract for structured LLM analysis;
- an asynchronous `Analyzer` port and deterministic fake;
- a pure conversation state machine with deterministic anomaly counting,
  takeover, silence, close, and operator reactivation transitions.

The LLM output is treated as an untrusted proposal. `proposed_action` is never
executed directly. `handle_analysis` returns a policy-approved
`effective_action`; a threshold-reaching anomaly returns
`escalate_to_human`, while a later result received in `human_takeover` or
`closed_not_interested` returns no action and remains silent. An accepted
`mark_not_interested` is returned once when the conversation enters the closed
state; it is not re-opened by customer text or model output.

The state machine reads only the validated `AnalysisResult`, not raw customer
text, and performs no I/O. `off_topic` and `is_dissatisfied` share one
consecutive-issue counter; both signals in one turn count once, and a normal
turn resets it. The second consecutive issue has priority over every model
proposal. `decision_note` is a short observability label, not chain-of-thought
and never an authorization signal.

Live LLM integration, the action executor, rolling rate limiting, a dialogue
endpoint/UI, and adversarial execution evidence are intentionally deferred to
Phases 3–8.

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
