"""Deterministic conversation state transitions.

The state machine deliberately knows nothing about the LLM, customer text, or
external side effects.  It consumes a validated ``AnalysisResult`` and
returns a transition that a later application layer may persist and execute.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from lead_qualification_agent.domain.models import (
    Action,
    AnalysisResult,
    ConversationStatus,
    Intent,
)


ISSUE_STREAK_THRESHOLD = 2


class TransitionEvent(StrEnum):
    """Auditable labels for state-machine outcomes."""

    NORMAL = "normal"
    ISSUE_RECORDED = "issue_recorded"
    FORCED_ESCALATION = "forced_escalation"
    MODEL_ESCALATION = "model_escalation"
    CLOSED_NOT_INTERESTED = "closed_not_interested"
    SILENT_HUMAN_TAKEOVER = "silent_human_takeover"
    SILENT_CLOSED = "silent_closed"
    HUMAN_REACTIVATED = "human_reactivated"
    REACTIVATION_IGNORED = "reactivation_ignored"
    REACTIVATION_REJECTED = "reactivation_rejected"


@dataclass(frozen=True, slots=True)
class ConversationState:
    """The minimum persisted state needed for deterministic policy decisions."""

    status: ConversationStatus = ConversationStatus.ACTIVE
    issue_streak: int = 0
    # Persistence layers may use this optimistic-concurrency token.  It is
    # intentionally excluded from business-state equality so Phase 2 callers
    # can reason about status and streak independently of storage metadata.
    revision: int = field(default=0, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, ConversationStatus):
            raise TypeError("status must be a ConversationStatus")
        if type(self.issue_streak) is not int:
            raise TypeError("issue_streak must be an integer")
        if type(self.revision) is not int:
            raise TypeError("revision must be an integer")
        if self.revision < 0:
            raise ValueError("revision must be non-negative")
        if not 0 <= self.issue_streak <= ISSUE_STREAK_THRESHOLD:
            raise ValueError(
                f"issue_streak must be between 0 and {ISSUE_STREAK_THRESHOLD}"
            )
        if (
            self.status is ConversationStatus.ACTIVE
            and self.issue_streak == ISSUE_STREAK_THRESHOLD
        ):
            raise ValueError(
                "an active conversation cannot retain a threshold-reaching streak"
            )
        if (
            self.status is ConversationStatus.CLOSED_NOT_INTERESTED
            and self.issue_streak != 0
        ):
            raise ValueError("a closed conversation must have a zero issue streak")


@dataclass(frozen=True, slots=True)
class StateTransition:
    """A pure result consumed by persistence and action-execution layers later.

    ``effective_action`` is the policy-approved action for this turn.  An
    escalation or close action is still returned once so the next layer can
    perform that transition, while ``silent`` records that no customer-facing
    reply should be emitted.  Once a conversation is already non-active,
    ``effective_action`` is ``None`` and the transition is fully silent.
    """

    previous_state: ConversationState
    next_state: ConversationState
    event: TransitionEvent
    effective_action: Action | None
    silent: bool
    reason: str


def handle_analysis(
    state: ConversationState,
    result: AnalysisResult,
) -> StateTransition:
    """Apply one validated analysis result without performing any side effect.

    ``result.proposed_action`` is an untrusted model proposal.  Only an action
    returned as ``effective_action`` may be considered by a later executor.
    Once a conversation is human-owned or closed, the result is intentionally
    ignored and the transition is silent.
    """

    if state.status is ConversationStatus.HUMAN_TAKEOVER:
        return StateTransition(
            previous_state=state,
            next_state=state,
            event=TransitionEvent.SILENT_HUMAN_TAKEOVER,
            effective_action=None,
            silent=True,
            reason="human_takeover_blocks_automatic_actions",
        )

    if state.status is ConversationStatus.CLOSED_NOT_INTERESTED:
        return StateTransition(
            previous_state=state,
            next_state=state,
            event=TransitionEvent.SILENT_CLOSED,
            effective_action=None,
            silent=True,
            reason="closed_conversation_blocks_automatic_actions",
        )

    issue_detected = (
        result.intent is Intent.OFF_TOPIC or result.is_dissatisfied
    )
    next_streak = (
        min(state.issue_streak + 1, ISSUE_STREAK_THRESHOLD)
        if issue_detected
        else 0
    )

    # The deterministic threshold takes precedence over every model proposal,
    # including mark_not_interested and an explicit model escalation.
    if next_streak == ISSUE_STREAK_THRESHOLD:
        next_state = ConversationState(
            status=ConversationStatus.HUMAN_TAKEOVER,
            issue_streak=ISSUE_STREAK_THRESHOLD,
            revision=state.revision + 1,
        )
        return StateTransition(
            previous_state=state,
            next_state=next_state,
            event=TransitionEvent.FORCED_ESCALATION,
            effective_action=Action.ESCALATE_TO_HUMAN,
            silent=True,
            reason="two_consecutive_issue_turns_force_human_takeover",
        )

    if result.proposed_action is Action.ESCALATE_TO_HUMAN:
        next_state = ConversationState(
            status=ConversationStatus.HUMAN_TAKEOVER,
            issue_streak=next_streak,
            revision=state.revision + 1,
        )
        return StateTransition(
            previous_state=state,
            next_state=next_state,
            event=TransitionEvent.MODEL_ESCALATION,
            effective_action=Action.ESCALATE_TO_HUMAN,
            silent=True,
            reason="model_escalation_accepted_by_policy",
        )

    if result.proposed_action is Action.MARK_NOT_INTERESTED:
        next_state = ConversationState(
            status=ConversationStatus.CLOSED_NOT_INTERESTED,
            issue_streak=0,
            revision=state.revision + 1,
        )
        return StateTransition(
            previous_state=state,
            next_state=next_state,
            event=TransitionEvent.CLOSED_NOT_INTERESTED,
            effective_action=Action.MARK_NOT_INTERESTED,
            silent=True,
            reason="model_marked_conversation_not_interested",
        )

    next_state = ConversationState(
        status=ConversationStatus.ACTIVE,
        issue_streak=next_streak,
        revision=state.revision + 1,
    )
    return StateTransition(
        previous_state=state,
        next_state=next_state,
        event=(
            TransitionEvent.ISSUE_RECORDED
            if issue_detected
            else TransitionEvent.NORMAL
        ),
        effective_action=result.proposed_action,
        silent=result.proposed_action is not Action.REPLY,
        reason=(
            "one_issue_turn_recorded"
            if issue_detected
            else "normal_turn_resets_issue_streak"
        ),
    )


def reactivate(state: ConversationState) -> StateTransition:
    """Handle an explicit operator reactivation request.

    Customer text and model output never call this function.  A closed
    conversation is terminal for this state machine; reopening it, if ever
    needed, must be a separate, explicit product operation.
    """

    if state.status is ConversationStatus.HUMAN_TAKEOVER:
        next_state = ConversationState(revision=state.revision + 1)
        return StateTransition(
            previous_state=state,
            next_state=next_state,
            event=TransitionEvent.HUMAN_REACTIVATED,
            effective_action=None,
            silent=True,
            reason="operator_reactivated_conversation",
        )

    if state.status is ConversationStatus.CLOSED_NOT_INTERESTED:
        return StateTransition(
            previous_state=state,
            next_state=state,
            event=TransitionEvent.REACTIVATION_REJECTED,
            effective_action=None,
            silent=True,
            reason="closed_conversation_requires_a_separate_reopen_operation",
        )

    return StateTransition(
        previous_state=state,
        next_state=state,
        event=TransitionEvent.REACTIVATION_IGNORED,
        effective_action=None,
        silent=True,
        reason="conversation_is_already_active",
    )
