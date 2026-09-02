from __future__ import annotations

from pathlib import Path
import multiprocessing as mp
from queue import Empty

from lead_qualification_agent.adapters.sqlite import (
    EventOutcome,
    SQLiteSessionStore,
)
from lead_qualification_agent.application.executor import (
    ActionExecutor,
    OutboundGateway,
)
from lead_qualification_agent.domain import (
    Action,
    AnalysisResult,
    ConversationState,
    handle_analysis,
)


class NoopSender:
    """A process-local sender; the database is the shared evidence source."""

    def send(self, customer_id: str, content: str) -> None:
        del customer_id, content


def _reply_worker(
    database_path: str,
    customer_id: str,
    barrier,
    outcomes,
) -> None:
    """Run one worker with a fresh connection in a spawned process."""

    try:
        store = SQLiteSessionStore(
            Path(database_path),
            timeout=15.0,
            busy_timeout_ms=15_000,
        )
        gateway = OutboundGateway(
            store,
            NoopSender(),
            clock=lambda: 1_000.0,
        )
        executor = ActionExecutor(store, gateway)
        analysis = AnalysisResult(
            intent="interested",
            is_dissatisfied=False,
            proposed_action=Action.REPLY,
            reply_draft="concurrent reply",
            decision_note="concurrency fixture",
        )
        transition = handle_analysis(ConversationState(), analysis)
        barrier.wait(timeout=20.0)
        execution = executor.execute(
            customer_id,
            transition,
            reply_draft=analysis.reply_draft,
            now=1_000.0,
        )
        outcomes.put(execution.outcome.value)
    except BaseException as exc:
        outcomes.put(f"error:{type(exc).__name__}")
        raise


def _run_competing_workers(
    context,
    database_path: Path,
    customer_id: str,
    worker_count: int = 8,
) -> list[str]:
    barrier = context.Barrier(worker_count)
    outcomes = context.Queue()
    processes = [
        context.Process(
            target=_reply_worker,
            args=(str(database_path), customer_id, barrier, outcomes),
        )
        for _ in range(worker_count)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30.0)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5.0)

    assert all(process.exitcode == 0 for process in processes)
    collected: list[str] = []
    for _ in processes:
        try:
            collected.append(outcomes.get(timeout=5.0))
        except Empty as exc:
            raise AssertionError("a worker did not report an outcome") from exc
    outcomes.close()
    return collected


def test_spawned_workers_share_one_reply_slot(tmp_path) -> None:
    database_path = tmp_path / "concurrent.sqlite3"
    store = SQLiteSessionStore(database_path)
    context = mp.get_context("spawn")

    for round_number in range(3):
        customer_id = f"concurrent-customer-{round_number}"
        outcomes = _run_competing_workers(
            context,
            database_path,
            customer_id,
        )
        events = store.list_events(customer_id)

        assert len(outcomes) == 8
        assert not any(outcome.startswith("error:") for outcome in outcomes)
        assert outcomes.count("sent") == 1
        assert sum(event.outcome is EventOutcome.SENT for event in events) == 1
        assert len(events) == 8
