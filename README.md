# KapibalaAI Lead Qualification Agent

A small, auditable agent that uses an LLM for lead-intent analysis while enforcing business constraints in deterministic application code.

## Current phase

Phases 0, 1, 2, and 3 are complete:

- runnable FastAPI project skeleton and health endpoint;
- closed enums for the five intents, four allowed actions, and conversation states;
- a Pydantic contract for structured LLM analysis;
- an asynchronous `Analyzer` port and deterministic fake;
- a pure conversation state machine with deterministic anomaly counting,
  takeover, silence, close, and operator reactivation transitions.
- a SQLite session/event store and an explicit action executor;
- an atomic rolling-window outbound gateway that is safe across worker
  processes.

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
proposal. Its immutable `revision` is an optimistic-concurrency token for the
persistence layer. `decision_note` is a short observability label, not
chain-of-thought and never an authorization signal.

## Phase 3 execution boundary

`ActionExecutor` accepts a `StateTransition`, never a raw
`proposed_action`. It uses an explicit branch for each of the four closed
actions; customer text cannot name a function or tool. The SQLite store
re-reads the session inside `BEGIN IMMEDIATE` and compares status, issue streak,
and revision before applying a transition. A stale transition is recorded and
cannot send.

- `reply` requires an active session and a non-empty draft. `OutboundGateway`
  atomically updates the session's `last_sent_at` only when the elapsed time is
  at least 60 seconds (or there has never been a send), then calls the injected
  sender. A reservation is made before the external call, so a crash sacrifices
  one window rather than allowing a retry to duplicate a message.
- `schedule_followup` records a scheduled event and does not consume the reply
  window.
- `escalate_to_human` and `mark_not_interested` record their action once and
  update the state without sending a customer-visible reply.
- A human-takeover or closed session returns no effective action and remains
  silent, even if a caller fabricates a reply transition.

`tests/test_concurrency.py` uses a real file-backed SQLite database, eight
spawned processes, independent connections, and synchronized attempts. The
test asserts that each competing customer receives exactly one successful
outbound event. Boundary tests cover immediate retry (0 seconds), 59.9 seconds,
and exactly 60 seconds.

Live LLM integration, a dialogue endpoint/UI, and adversarial execution
evidence are intentionally deferred to Phases 4–8.

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
