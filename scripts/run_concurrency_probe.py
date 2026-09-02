"""Reproduce the multi-process rolling reply limit with public-safe output."""

from __future__ import annotations

from collections import Counter
import json
import multiprocessing as mp
from pathlib import Path
from queue import Empty
from tempfile import TemporaryDirectory

from lead_qualification_agent.adapters.sqlite import (
    EventOutcome,
    SQLiteSessionStore,
)
from lead_qualification_agent.application.executor import (
    ActionExecutor,
    ExecutionOutcome,
    OutboundGateway,
)
from lead_qualification_agent.domain import (
    Action,
    AnalysisResult,
    ConversationState,
    handle_analysis,
)


WORKER_COUNT = 8
CUSTOMER_ID = "concurrency-probe-customer"
FIXED_TIME = 1_000.0


class NoopSender:
    """Represent one outbound channel without printing customer content."""

    def send(self, customer_id: str, content: str) -> None:
        del customer_id, content


def _reply_analysis() -> AnalysisResult:
    return AnalysisResult(
        intent="interested",
        is_dissatisfied=False,
        proposed_action=Action.REPLY,
        reply_draft="public-safe probe reply",
        decision_note="concurrency probe",
    )


def _worker(
    database_path: str,
    start_barrier,
    outcomes,
) -> None:
    """Use a process-local store and retry only expected stale transitions."""

    try:
        store = SQLiteSessionStore(
            Path(database_path),
            timeout=15.0,
            busy_timeout_ms=15_000,
        )
        executor = ActionExecutor(
            store,
            OutboundGateway(store, NoopSender(), clock=lambda: FIXED_TIME),
        )
        analysis = _reply_analysis()
        stale_retries = 0

        start_barrier.wait(timeout=20.0)
        while True:
            session = store.get_session(CUSTOMER_ID)
            state = ConversationState() if session is None else session.state
            execution = executor.execute(
                CUSTOMER_ID,
                handle_analysis(state, analysis),
                reply_draft=analysis.reply_draft,
                now=FIXED_TIME,
            )
            if execution.outcome is not ExecutionOutcome.STALE:
                outcomes.put(
                    {
                        "outcome": execution.outcome.value,
                        "message_sent": execution.message_sent,
                        "stale_retries": stale_retries,
                    }
                )
                return
            stale_retries += 1
    except BaseException as exc:
        outcomes.put({"error": type(exc).__name__})
        raise


def run_probe() -> dict[str, object]:
    """Run eight spawned workers and return a deterministic public summary."""

    context = mp.get_context("spawn")
    with TemporaryDirectory(prefix="kapibala-concurrency-") as directory:
        database_path = Path(directory) / "probe.sqlite3"
        store = SQLiteSessionStore(database_path)
        start_barrier = context.Barrier(WORKER_COUNT)
        outcomes = context.Queue()
        processes = [
            context.Process(
                target=_worker,
                args=(str(database_path), start_barrier, outcomes),
            )
            for _ in range(WORKER_COUNT)
        ]

        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=30.0)
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5.0)

        reports: list[dict[str, object]] = []
        for _ in processes:
            try:
                reports.append(outcomes.get(timeout=5.0))
            except Empty:
                reports.append({"error": "missing_worker_report"})
        outcomes.close()

        outcome_counts = Counter(
            report["outcome"] for report in reports if "outcome" in report
        )
        events = store.list_events(CUSTOMER_ID)
        persisted_counts = Counter(event.outcome.value for event in events)
        session = store.get_session(CUSTOMER_ID)
        worker_errors = sorted(
            str(report["error"]) for report in reports if "error" in report
        )
        exit_codes = [process.exitcode for process in processes]
        stale_retries = sum(
            int(report.get("stale_retries", 0)) for report in reports
        )

        passed = (
            exit_codes == [0] * WORKER_COUNT
            and not worker_errors
            and outcome_counts == {"sent": 1, "rate_limited": 7}
            and sum(bool(report.get("message_sent")) for report in reports) == 1
            and persisted_counts[EventOutcome.SENT.value] == 1
            and persisted_counts[EventOutcome.RATE_LIMITED.value] == 7
            and persisted_counts[EventOutcome.STALE.value] == stale_retries
            and session is not None
            and session.state.revision == WORKER_COUNT
        )

        return {
            "probe": "multi_process_rolling_rate_limit",
            "passed": passed,
            "workers": WORKER_COUNT,
            "process_start_method": "spawn",
            "storage": "temporary_file_backed_sqlite",
            "worker_outcomes": {
                "sent": outcome_counts["sent"],
                "rate_limited": outcome_counts["rate_limited"],
            },
            "message_sent_true": sum(
                bool(report.get("message_sent")) for report in reports
            ),
            "persisted_terminal_events": {
                "sent": persisted_counts[EventOutcome.SENT.value],
                "rate_limited": persisted_counts[EventOutcome.RATE_LIMITED.value],
            },
            "transient_conflicts": {
                "worker_retry_attempts": stale_retries,
                "persisted_stale_events": persisted_counts[
                    EventOutcome.STALE.value
                ],
            },
            "final_revision": None if session is None else session.state.revision,
            "worker_errors": worker_errors,
        }


def main() -> int:
    summary = run_probe()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
