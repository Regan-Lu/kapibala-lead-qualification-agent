# Collaboration and AI use

## Submission ownership

- Candidate: **陆铮 (Lu Zheng)**
- Team form: solo submission, following the assessment-group clarification that
  one-person teams are allowed
- Repository, scope decisions, acceptance, and presentation responsibility:
  陆铮

Codex was used as a coding agent, not as a second candidate or a source of
unverified authority. This is a one-candidate submission with AI-assisted
development.

## What the coding agent helped with

Codex assisted with:

- decomposing the prompt into explicit invariants and implementation phases;
- comparing a direct Gemini API integration with a framework-based approach;
- drafting and revising Python, tests, the browser demo, scripts, and documents;
- reviewing concurrency, model-output, API, and disclosure boundaries;
- running repeatable local checks and interpreting failures.

The accepted result was checked with repository tests, direct inspection,
live-model runs where applicable, and browser acceptance. I remain responsible
for explaining the resulting code and its trade-offs during the defense.

## Verification and correction loop

The project used different evidence for different claims:

1. Pure state-machine and contract tests establish deterministic policy.
2. File-backed SQLite tests and the standalone probe use spawned processes and
   independent connections to attack the real transaction boundary.
3. Live Gemini scripts confirm that the configured API and structured-output
   path execute, while acknowledging that hosted-model choices can vary.
4. Browser checks confirm that unsent drafts are not rendered as Agent speech.
5. Clean-environment installation, tests, packaging, and startup are repeated
   before submission.

A concrete correction happened during adversarial testing. The first live run
did not reproduce every intended observation: some wording invited
`schedule_followup`, and the public response did not distinguish model behavior
from model-service failure. Instead of changing business rules to force a pass,
the runner was changed to expose only aggregate per-stage call/failure counts,
generated text was removed from evidence output, and ambiguous test messages
were rewritten. The next run had zero model-call failures and reproduced all
targets. Full details are in
[`evidence/phase7-adversarial-results.md`](evidence/phase7-adversarial-results.md#what-the-first-live-run-caught).

## Commit progression

The Git history is intentionally incremental:

| Phase | Main deliverable |
| --- | --- |
| 1 | Public project skeleton, health check, and validated analysis contract |
| 2 | Pure deterministic conversation state machine |
| 3 | SQLite persistence, closed action executor, rolling limiter, and process contention test |
| 4 | Direct Gemini integration, structured reply review, and live smoke check |
| 5 | End-to-end conversation API and operator controls |
| 6 | Minimal browser demo and UI boundary tests |
| 7 | Live adversarial suite, multi-process probe, and sanitized evidence |
| 8 | Final architecture, constraint mapping, clean-environment acceptance, and defense guide |

## Time record

Estimated active effort was approximately 12–15 hours across two calendar days.
This is a retrospective range because no separate timer was used. Before the
final documentation commit, the verifiable Git author-timestamp span was
2026-09-01 23:26:05 to 2026-09-02 22:39:16 (Asia/Shanghai), or 23 hours
13 minutes. It includes an overnight break, model waiting, browser checks, and
documentation and is not a claim of continuous coding time.
