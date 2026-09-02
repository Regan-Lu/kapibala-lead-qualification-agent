# KapibalaAI Lead Qualification Agent

A small, auditable agent that uses an LLM for lead-intent analysis while enforcing business constraints in deterministic application code.

## Current phase

Phases 0 through 5 are implemented:

- runnable FastAPI project skeleton and health endpoint;
- closed enums for the five intents, four allowed actions, and conversation states;
- a Pydantic contract for structured LLM analysis;
- an asynchronous `Analyzer` port and deterministic fake;
- a pure conversation state machine with deterministic anomaly counting,
  takeover, silence, close, and operator reactivation transitions.
- a SQLite session/event store and an explicit action executor;
- an atomic rolling-window outbound gateway that is safe across worker
  processes;
- a direct Gemini Interactions REST adapter with structured JSON output;
- a separately instructed reply-review call and non-disclosing analysis service.
- one `ConversationService` path from inbound text through guarded analysis,
  deterministic policy, persistence, and action execution;
- customer-facing conversation endpoints and token-protected operator controls.

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

## Phase 4 model and disclosure boundary

The project calls Gemini's stable `v1` Interactions REST endpoint directly,
without a provider SDK or agent framework. The request contains no tools or
function declarations.
Customer messages and the last eight history entries are serialized into the
untrusted `input` field; they are never interpolated into the system
instruction. Gemini is asked for JSON matching `AnalysisResult`, and the
response is validated again locally by Pydantic before it reaches the state
machine.

For a proposed `reply`, the `GuardedAnalysisService` path requires a second
structured review over the customer request and candidate draft, using a
separate instruction and `ReplyReview` contract. A blocked reply is converted
by code into `escalate_to_human` with no draft. If analysis or review is
temporarily unavailable, the service emits no reply and chooses
`schedule_followup`, keeping the conversation active instead of turning a
transient infrastructure failure into permanent human takeover. Non-reply
actions skip review because they cannot expose customer-facing text.

Phase 5 exposes this path through one `ConversationService` composition root.
The customer API accepts only message content; it never accepts a
caller-supplied action, reply draft, state, history, model setting, or API key.
Calling `ActionExecutor` directly remains an internal trust boundary, not a
customer-facing path.

The primary disclosure defense is data minimization: the model input contains
a small public capability summary and no credentials, private price floors,
contracts, customer lists, or internal operating data. The separate review is
a semantic second line, not a claim of perfect prompt-injection detection. Its
known limitation is model misclassification; guarded handoff and the
deterministic action/state boundaries limit the consequence of that error.

The raw REST client uses a 30-second timeout and performs no automatic retry.
A transient API failure therefore becomes a silent `schedule_followup` result;
bounded retry policy is intentionally deferred beyond this phase.

A live smoke script is included. It requires outbound HTTPS access to Google's
Gemini endpoint:

```bash
cp .env.example .env
# Fill GEMINI_API_KEY with a Google AI Studio authorization key, then load it
# without printing it.
set -a
source .env
set +a
python scripts/live_gemini_smoke.py
```

Live verification on 2026-09-02 completed exactly two model calls: one intent
analysis and one separately instructed reply review. The final result was an
allowed `reply`. The credential remained only in the ignored local `.env`.

The adapter targets Google's stable `v1` Interactions REST API and current
top-level `response_format` contract: [Interactions API reference](https://ai.google.dev/api/interactions-api-v1),
[API versions](https://ai.google.dev/gemini-api/docs/api-versions), and
[structured outputs](https://ai.google.dev/gemini-api/docs/structured-output).

A browser UI and adversarial execution evidence are intentionally deferred to
Phases 6–8.

## Phase 5 conversation API

The API surface is deliberately small:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Process health; does not require Gemini configuration. |
| `POST` | `/conversations/{customer_id}/messages` | Submit one customer message through the complete guarded path. |
| `GET` | `/conversations/{customer_id}` | Read an existing conversation snapshot and recent action outcomes. |
| `POST` | `/operator/conversations/{customer_id}/reactivate` | Return a human-takeover conversation to active handling. |
| `POST` | `/operator/demo/reset` | Clear local demo sessions and action events. |

The message request body contains only `message`:

```bash
curl -sS -X POST http://127.0.0.1:8000/conversations/demo-001/messages \
  -H 'Content-Type: application/json' \
  --data '{"message":"What can your lead-qualification product do?"}'
```

A turn response reports `customer_id`, validated `intent` and
`is_dissatisfied`, the policy-approved `action`, execution `outcome`,
`message_sent`, the customer-visible `reply` when one was actually sent, and
the persisted `status`, `issue_streak`, and `revision`. A stale concurrent turn
returns the same response shape with HTTP 409. The snapshot endpoint accepts an
optional `event_limit` query parameter from 1 to 200 (default 50) and returns
the state plus recent events containing only event ID, action, outcome, and
timestamp. Neither response includes analysis notes, review details,
state-machine reasons, model requests, or raw model output.

Both operator endpoints require the `X-Operator-Token` request header. Configure
the expected value with the server-side `OPERATOR_TOKEN` environment variable;
do not place it in a JSON body or URL:

```bash
curl -sS -X POST http://127.0.0.1:8000/operator/demo/reset \
  -H "X-Operator-Token: $OPERATOR_TOKEN"

curl -sS -X POST \
  http://127.0.0.1:8000/operator/conversations/demo-001/reactivate \
  -H "X-Operator-Token: $OPERATOR_TOKEN"
```

A missing or incorrect supplied token returns HTTP 401. If the server has no
`OPERATOR_TOKEN` configured, operator controls return a generic HTTP 503.

`GEMINI_API_KEY` is read only from the server environment. If it is absent,
the process still starts: `/health` and queries for already persisted
conversations remain available. Messages for a new or `active` conversation
return a generic `503 Service Unavailable` response without credential values
or exception details, and a new customer does not leave an empty session
behind; an existing `human_takeover` or `closed_not_interested` conversation
still returns its deterministic silent result before any model call.

### Suggested live demonstration

1. Start the service with `GEMINI_API_KEY` and `OPERATOR_TOKEN` loaded from the
   ignored local `.env`, then verify `/health`.
2. Reset demo state with the token-protected reset endpoint.
3. Submit a normal product question and inspect the sent reply and active
   status.
4. Submit another reply-producing message within 60 seconds to demonstrate the
   per-customer rolling limit.
5. Submit two consecutive off-topic or dissatisfied turns to enter
   `human_takeover`, then submit one more message to demonstrate silence.
6. Query the conversation, reactivate it through the operator endpoint, and
   submit a final normal message.

Live HTTP verification on 2026-09-02 returned 200 from `/health`; the first
customer message was analyzed by Gemini, passed the independent reply review,
and produced `sent`. A second reply attempt about 16 seconds later produced
`rate_limited` with `reply: null`. The snapshot showed the `sent` and
`rate_limited` events, and the token-protected reset deleted one session and
two events.

Phase 5 intentionally does not persist a complete conversation transcript.
The current model call analyzes the newest customer message without restoring
full prior dialogue history. Session state, action outcomes, and the simulated
outbound channel are sufficient for the constraint-focused demo; durable
multi-turn message history is deferred rather than accepted from an
untrusted caller.

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

Never commit `.env` or a real API key. The repository ignores `.env` from its
first commit.
