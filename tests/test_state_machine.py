import pytest

from lead_qualification_agent.domain import (
    Action,
    AnalysisResult,
    ConversationState,
    ConversationStatus,
    Intent,
    ISSUE_STREAK_THRESHOLD,
    TransitionEvent,
    handle_analysis,
    reactivate,
)


def analysis(
    *,
    intent: Intent = Intent.INTERESTED,
    dissatisfied: bool = False,
    action: Action = Action.REPLY,
) -> AnalysisResult:
    return AnalysisResult(
        intent=intent,
        is_dissatisfied=dissatisfied,
        proposed_action=action,
        reply_draft="A helpful reply." if action is Action.REPLY else None,
        decision_note="Deterministic test fixture.",
    )


def test_normal_turn_keeps_active_and_clears_streak() -> None:
    state = ConversationState(
        status=ConversationStatus.ACTIVE,
        issue_streak=1,
    )

    transition = handle_analysis(state, analysis())

    assert transition.next_state == ConversationState()
    assert transition.event is TransitionEvent.NORMAL
    assert transition.effective_action is Action.REPLY
    assert transition.silent is False


@pytest.mark.parametrize(
    "result",
    [
        analysis(intent=Intent.OFF_TOPIC),
        analysis(dissatisfied=True),
    ],
)
def test_each_issue_signal_starts_one_shared_streak(result: AnalysisResult) -> None:
    transition = handle_analysis(ConversationState(), result)

    assert transition.next_state == ConversationState(
        status=ConversationStatus.ACTIVE,
        issue_streak=1,
    )
    assert transition.event is TransitionEvent.ISSUE_RECORDED


def test_two_issue_signals_in_one_turn_count_once() -> None:
    result = analysis(intent=Intent.OFF_TOPIC, dissatisfied=True)

    transition = handle_analysis(ConversationState(), result)

    assert transition.next_state == ConversationState(
        status=ConversationStatus.ACTIVE,
        issue_streak=1,
    )
    assert transition.event is TransitionEvent.ISSUE_RECORDED


def test_normal_turn_resets_streak_before_a_later_issue() -> None:
    first = handle_analysis(
        ConversationState(),
        analysis(intent=Intent.OFF_TOPIC),
    )
    second = handle_analysis(first.next_state, analysis())
    third = handle_analysis(
        second.next_state,
        analysis(dissatisfied=True),
    )

    assert second.next_state == ConversationState()
    assert third.next_state.issue_streak == 1
    assert third.next_state.status is ConversationStatus.ACTIVE


@pytest.mark.parametrize("action", list(Action))
def test_second_issue_forces_takeover_over_every_model_action(
    action: Action,
) -> None:
    state = ConversationState(
        status=ConversationStatus.ACTIVE,
        issue_streak=1,
    )

    transition = handle_analysis(
        state,
        analysis(intent=Intent.OFF_TOPIC, action=action),
    )

    assert transition.next_state == ConversationState(
        status=ConversationStatus.HUMAN_TAKEOVER,
        issue_streak=ISSUE_STREAK_THRESHOLD,
    )
    assert transition.event is TransitionEvent.FORCED_ESCALATION
    assert transition.effective_action is Action.ESCALATE_TO_HUMAN
    assert transition.silent is True


def test_model_escalation_takes_over_before_threshold() -> None:
    transition = handle_analysis(
        ConversationState(),
        analysis(action=Action.ESCALATE_TO_HUMAN),
    )

    assert transition.next_state == ConversationState(
        status=ConversationStatus.HUMAN_TAKEOVER,
        issue_streak=0,
    )
    assert transition.event is TransitionEvent.MODEL_ESCALATION
    assert transition.effective_action is Action.ESCALATE_TO_HUMAN
    assert transition.silent is True


def test_schedule_followup_is_allowed_but_sends_no_reply() -> None:
    transition = handle_analysis(
        ConversationState(),
        analysis(action=Action.SCHEDULE_FOLLOWUP),
    )

    assert transition.next_state == ConversationState()
    assert transition.effective_action is Action.SCHEDULE_FOLLOWUP
    assert transition.silent is True


def test_mark_not_interested_closes_when_threshold_is_not_reached() -> None:
    transition = handle_analysis(
        ConversationState(),
        analysis(action=Action.MARK_NOT_INTERESTED),
    )

    assert transition.next_state == ConversationState(
        status=ConversationStatus.CLOSED_NOT_INTERESTED,
        issue_streak=0,
    )
    assert transition.event is TransitionEvent.CLOSED_NOT_INTERESTED
    assert transition.effective_action is Action.MARK_NOT_INTERESTED
    assert transition.silent is True


@pytest.mark.parametrize(
    "status",
    [
        ConversationStatus.HUMAN_TAKEOVER,
        ConversationStatus.CLOSED_NOT_INTERESTED,
    ],
)
def test_non_active_state_ignores_later_model_results(status: ConversationStatus) -> None:
    state = ConversationState(
        status=status,
        issue_streak=(
            ISSUE_STREAK_THRESHOLD
            if status is ConversationStatus.HUMAN_TAKEOVER
            else 0
        ),
    )

    transition = handle_analysis(
        state,
        analysis(
            intent=Intent.OFF_TOPIC,
            dissatisfied=True,
            action=Action.REPLY,
        ),
    )

    assert transition.next_state is state
    assert transition.effective_action is None
    assert transition.silent is True
    assert transition.event in {
        TransitionEvent.SILENT_HUMAN_TAKEOVER,
        TransitionEvent.SILENT_CLOSED,
    }


def test_human_reactivation_resets_streak_and_returns_to_active() -> None:
    state = ConversationState(
        status=ConversationStatus.HUMAN_TAKEOVER,
        issue_streak=ISSUE_STREAK_THRESHOLD,
    )

    transition = reactivate(state)

    assert transition.next_state == ConversationState()
    assert transition.event is TransitionEvent.HUMAN_REACTIVATED
    assert transition.effective_action is None


def test_reactivation_cannot_reopen_closed_conversation() -> None:
    state = ConversationState(
        status=ConversationStatus.CLOSED_NOT_INTERESTED,
    )

    transition = reactivate(state)

    assert transition.next_state is state
    assert transition.event is TransitionEvent.REACTIVATION_REJECTED
    assert transition.effective_action is None
    assert transition.silent is True


def test_reactivation_of_active_conversation_is_a_noop() -> None:
    state = ConversationState()

    transition = reactivate(state)

    assert transition.next_state is state
    assert transition.event is TransitionEvent.REACTIVATION_IGNORED


@pytest.mark.parametrize(
    "state",
    [
        ConversationState(
            status=ConversationStatus.ACTIVE,
            issue_streak=1,
        ),
        ConversationState(
            status=ConversationStatus.HUMAN_TAKEOVER,
            issue_streak=ISSUE_STREAK_THRESHOLD,
        ),
    ],
)
def test_takeover_remains_silent_for_any_future_result(
    state: ConversationState,
) -> None:
    if state.status is ConversationStatus.ACTIVE:
        state = handle_analysis(
            state,
            analysis(intent=Intent.OFF_TOPIC),
        ).next_state

    transition = handle_analysis(
        state,
        analysis(action=Action.MARK_NOT_INTERESTED),
    )

    assert transition.next_state.status is ConversationStatus.HUMAN_TAKEOVER
    assert transition.effective_action is None
    assert transition.silent is True
