from dataclasses import dataclass, field

import pytest

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
    ConversationStatus,
    Intent,
    StateTransition,
    TransitionEvent,
    handle_analysis,
    reactivate,
)


@dataclass
class RecordingSender:
    messages: list[tuple[str, str]] = field(default_factory=list)

    def send(self, customer_id: str, content: str) -> None:
        self.messages.append((customer_id, content))


@dataclass
class FailingSender:
    calls: int = 0

    def send(self, customer_id: str, content: str) -> None:
        del customer_id, content
        self.calls += 1
        raise RuntimeError("simulated sender failure")


def result(
    *,
    intent: Intent = Intent.INTERESTED,
    dissatisfied: bool = False,
    action: Action = Action.REPLY,
) -> AnalysisResult:
    return AnalysisResult(
        intent=intent,
        is_dissatisfied=dissatisfied,
        proposed_action=action,
        reply_draft="A useful reply." if action is Action.REPLY else None,
        decision_note="Deterministic test fixture.",
    )


def build_executor(
    tmp_path,
    *,
    sender: RecordingSender | FailingSender | None = None,
    clock=None,
) -> tuple[SQLiteSessionStore, ActionExecutor, RecordingSender | FailingSender]:
    store = SQLiteSessionStore(tmp_path / "agent.sqlite3")
    sender = sender or RecordingSender()
    gateway = OutboundGateway(
        store,
        sender,
        clock=clock or (lambda: 1000.0),
    )
    return store, ActionExecutor(store, gateway), sender


def test_reply_is_sent_once_and_recorded(tmp_path) -> None:
    store, executor, sender = build_executor(tmp_path)
    analysis = result()
    transition = handle_analysis(ConversationState(), analysis)

    execution = executor.execute(
        "customer-1",
        transition,
        reply_draft=analysis.reply_draft,
        now=1000.0,
    )

    assert execution.outcome is ExecutionOutcome.SENT
    assert execution.message_sent is True
    assert sender.messages == [("customer-1", "A useful reply.")]
    session = store.get_session("customer-1")
    assert session is not None
    assert session.last_sent_at == 1000.0
    assert [event.outcome for event in store.list_events("customer-1")] == [
        EventOutcome.SENT
    ]


def test_rolling_window_uses_0_59_9_and_60_second_boundaries(tmp_path) -> None:
    class Clock:
        value = 1000.0

        def __call__(self) -> float:
            return self.value

    clock = Clock()
    store, executor, sender = build_executor(tmp_path, clock=clock)
    analysis = result()

    state = ConversationState()
    first = handle_analysis(state, analysis)
    sent = executor.execute(
        "customer-rolling",
        first,
        reply_draft=analysis.reply_draft,
    )

    current = store.get_session("customer-rolling")
    assert current is not None
    limited_immediately = executor.execute(
        "customer-rolling",
        handle_analysis(current.state, analysis),
        reply_draft=analysis.reply_draft,
    )

    current = store.get_session("customer-rolling")
    assert current is not None
    clock.value = 1059.9
    second = handle_analysis(current.state, analysis)
    limited = executor.execute(
        "customer-rolling",
        second,
        reply_draft=analysis.reply_draft,
    )

    current = store.get_session("customer-rolling")
    assert current is not None
    clock.value = 1060.0
    third = handle_analysis(current.state, analysis)
    sent_at_boundary = executor.execute(
        "customer-rolling",
        third,
        reply_draft=analysis.reply_draft,
    )

    assert sent.outcome is ExecutionOutcome.SENT
    assert limited_immediately.outcome is ExecutionOutcome.RATE_LIMITED
    assert limited.outcome is ExecutionOutcome.RATE_LIMITED
    assert sent_at_boundary.outcome is ExecutionOutcome.SENT
    assert len(sender.messages) == 2
    assert [event.outcome for event in store.list_events("customer-rolling")] == [
        EventOutcome.SENT,
        EventOutcome.RATE_LIMITED,
        EventOutcome.RATE_LIMITED,
        EventOutcome.SENT,
    ]


def test_rate_limit_is_per_customer_and_non_reply_actions_do_not_consume_it(
    tmp_path,
) -> None:
    store, executor, sender = build_executor(tmp_path)
    schedule = result(action=Action.SCHEDULE_FOLLOWUP)
    scheduled = executor.execute(
        "customer-a",
        handle_analysis(ConversationState(), schedule),
        now=1000.0,
    )
    reply = result()
    sent_a = executor.execute(
        "customer-a",
        handle_analysis(store.get_session("customer-a").state, reply),  # type: ignore[union-attr]
        reply_draft=reply.reply_draft,
        now=1000.0,
    )
    sent_b = executor.execute(
        "customer-b",
        handle_analysis(ConversationState(), reply),
        reply_draft=reply.reply_draft,
        now=1000.0,
    )

    assert scheduled.outcome is ExecutionOutcome.SCHEDULED
    assert sent_a.outcome is ExecutionOutcome.SENT
    assert sent_b.outcome is ExecutionOutcome.SENT
    assert len(sender.messages) == 2


def test_escalation_and_close_are_explicit_actions_but_send_no_message(tmp_path) -> None:
    store, executor, sender = build_executor(tmp_path)

    escalation = result(action=Action.ESCALATE_TO_HUMAN)
    escalated = executor.execute(
        "customer-escalate",
        handle_analysis(ConversationState(), escalation),
        now=1000.0,
    )
    assert escalated.outcome is ExecutionOutcome.ESCALATED
    assert escalated.action is Action.ESCALATE_TO_HUMAN
    assert escalated.message_sent is False

    closed_analysis = result(action=Action.MARK_NOT_INTERESTED)
    closed = executor.execute(
        "customer-close",
        handle_analysis(ConversationState(), closed_analysis),
        now=1000.0,
    )
    assert closed.outcome is ExecutionOutcome.CLOSED
    assert closed.action is Action.MARK_NOT_INTERESTED
    assert closed.message_sent is False
    assert sender.messages == []

    assert store.get_session("customer-escalate").state.status is ConversationStatus.HUMAN_TAKEOVER  # type: ignore[union-attr]
    assert store.get_session("customer-close").state.status is ConversationStatus.CLOSED_NOT_INTERESTED  # type: ignore[union-attr]


def test_non_active_state_cannot_be_bypassed_by_fabricated_reply_transition(
    tmp_path,
) -> None:
    store, executor, sender = build_executor(tmp_path)
    escalation = result(action=Action.ESCALATE_TO_HUMAN)
    takeover = executor.execute(
        "customer-gate",
        handle_analysis(ConversationState(), escalation),
        now=1000.0,
    )
    assert takeover.outcome is ExecutionOutcome.ESCALATED
    takeover_state = store.get_session("customer-gate").state  # type: ignore[union-attr]

    # Simulate a stale caller trying to use a reply action after takeover.
    fabricated = StateTransition(
        previous_state=takeover_state,
        next_state=ConversationState(
            status=ConversationStatus.HUMAN_TAKEOVER,
            issue_streak=takeover_state.issue_streak,
            revision=takeover_state.revision + 1,
        ),
        event=TransitionEvent.SILENT_HUMAN_TAKEOVER,
        effective_action=Action.REPLY,
        silent=False,
        reason="fabricated_transition",
    )
    blocked = executor.execute(
        "customer-gate",
        fabricated,
        reply_draft="must not send",
        now=1000.0,
    )

    assert blocked.outcome is ExecutionOutcome.SILENT
    assert blocked.message_sent is False
    assert sender.messages == []


def test_reactivation_is_persisted_and_closed_state_stays_terminal(tmp_path) -> None:
    store, executor, sender = build_executor(tmp_path)
    escalation = result(action=Action.ESCALATE_TO_HUMAN)
    executor.execute(
        "customer-reactivate",
        handle_analysis(ConversationState(), escalation),
        now=1000.0,
    )
    takeover_state = store.get_session("customer-reactivate").state  # type: ignore[union-attr]

    fabricated_unsilent_reactivation = StateTransition(
        previous_state=takeover_state,
        next_state=ConversationState(revision=takeover_state.revision + 1),
        event=TransitionEvent.HUMAN_REACTIVATED,
        effective_action=None,
        silent=False,
        reason="fabricated_unsilent_reactivation",
    )
    rejected = executor.execute(
        "customer-reactivate",
        fabricated_unsilent_reactivation,
        now=1000.5,
    )
    assert rejected.outcome is ExecutionOutcome.REJECTED
    assert rejected.state.status is ConversationStatus.HUMAN_TAKEOVER

    reactivated = executor.execute(
        "customer-reactivate",
        reactivate(takeover_state),
        now=1001.0,
    )
    assert reactivated.outcome is ExecutionOutcome.REACTIVATED
    assert reactivated.state.status is ConversationStatus.ACTIVE
    assert reactivated.state.issue_streak == 0

    close = result(action=Action.MARK_NOT_INTERESTED)
    executor.execute(
        "customer-terminal",
        handle_analysis(ConversationState(), close),
        now=1000.0,
    )
    closed_state = store.get_session("customer-terminal").state  # type: ignore[union-attr]
    blocked_reopen = executor.execute(
        "customer-terminal",
        reactivate(closed_state),
        now=1001.0,
    )

    assert blocked_reopen.outcome is ExecutionOutcome.SILENT
    assert blocked_reopen.state.status is ConversationStatus.CLOSED_NOT_INTERESTED
    assert sender.messages == []


def test_sender_failure_keeps_reserved_window_and_records_failure(tmp_path) -> None:
    failing = FailingSender()
    store, executor, _ = build_executor(tmp_path, sender=failing)
    analysis = result()
    first = executor.execute(
        "customer-failure",
        handle_analysis(ConversationState(), analysis),
        reply_draft=analysis.reply_draft,
        now=1000.0,
    )
    current = store.get_session("customer-failure")
    assert current is not None
    second = executor.execute(
        "customer-failure",
        handle_analysis(current.state, analysis),
        reply_draft=analysis.reply_draft,
        now=1001.0,
    )

    assert first.outcome is ExecutionOutcome.FAILED
    assert second.outcome is ExecutionOutcome.RATE_LIMITED
    assert failing.calls == 1
    assert [event.outcome for event in store.list_events("customer-failure")] == [
        EventOutcome.FAILED,
        EventOutcome.RATE_LIMITED,
    ]


def test_invalid_reply_draft_is_rejected_without_sender_call(tmp_path) -> None:
    store, executor, sender = build_executor(tmp_path)
    analysis = result()
    execution = executor.execute(
        "customer-invalid",
        handle_analysis(ConversationState(), analysis),
        reply_draft="   ",
        now=1000.0,
    )

    assert execution.outcome is ExecutionOutcome.REJECTED
    assert execution.message_sent is False
    assert sender.messages == []
    assert store.list_events("customer-invalid")[0].outcome is EventOutcome.REJECTED
