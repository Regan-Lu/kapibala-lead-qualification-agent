"""Explicit action execution and the rolling outbound rate limit."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable
import time

from lead_qualification_agent.adapters.sqlite import (
    EventOutcome,
    SQLiteSessionStore,
    StorageActionResult,
    StoredSession,
)
from lead_qualification_agent.domain import (
    Action,
    ConversationState,
    ConversationStatus,
    StateTransition,
    TransitionEvent,
)
from lead_qualification_agent.ports.outbound import OutboundSender


class ExecutionOutcome(StrEnum):
    """Stable outcomes returned to an API or demo layer."""

    SENT = "sent"
    RATE_LIMITED = "rate_limited"
    SCHEDULED = "scheduled"
    ESCALATED = "escalated"
    CLOSED = "closed"
    SILENT = "silent"
    STALE = "stale"
    FAILED = "failed"
    REJECTED = "rejected"
    REACTIVATED = "reactivated"


@dataclass(frozen=True, slots=True)
class ActionExecution:
    customer_id: str
    action: Action | None
    outcome: ExecutionOutcome
    message_sent: bool
    silent: bool
    event_id: int | None
    state: ConversationState
    detail: str


@dataclass(frozen=True, slots=True)
class OutboundDelivery:
    outcome: ExecutionOutcome
    message_sent: bool
    event_id: int | None
    session: StoredSession
    detail: str


class OutboundGateway:
    """Reserve a reply slot atomically before calling an external sender."""

    def __init__(
        self,
        store: SQLiteSessionStore,
        sender: OutboundSender,
        *,
        clock: Callable[[], float] = time.time,
        window_seconds: float = 60.0,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.store = store
        self.sender = sender
        self.clock = clock
        self.window_seconds = float(window_seconds)

    def send(
        self,
        customer_id: str,
        transition: StateTransition,
        content: str,
        *,
        now: float | None = None,
    ) -> OutboundDelivery:
        if not content.strip():
            raise ValueError("reply content must not be empty")

        timestamp = self.clock() if now is None else float(now)
        prepared = self.store.prepare_reply(
            customer_id,
            transition,
            now=timestamp,
            window_seconds=self.window_seconds,
        )
        if prepared.outcome is EventOutcome.STALE:
            return OutboundDelivery(
                outcome=ExecutionOutcome.STALE,
                message_sent=False,
                event_id=prepared.event_id,
                session=prepared.session,
                detail="state_transition_did_not_match_persisted_session",
            )
        if prepared.outcome is EventOutcome.RATE_LIMITED:
            return OutboundDelivery(
                outcome=ExecutionOutcome.RATE_LIMITED,
                message_sent=False,
                event_id=prepared.event_id,
                session=prepared.session,
                detail="rolling_60_second_limit",
            )
        if prepared.outcome is not EventOutcome.OUTBOUND_RESERVED:
            raise RuntimeError("unexpected reply preparation outcome")
        if prepared.event_id is None:
            raise RuntimeError("reply reservation did not return an event id")

        try:
            self.sender.send(customer_id, content)
        except Exception as exc:
            self.store.finalize_outbound(
                prepared.event_id,
                sent=False,
                now=self.clock() if now is None else timestamp,
                detail=f"sender_failed:{type(exc).__name__}",
            )
            return OutboundDelivery(
                outcome=ExecutionOutcome.FAILED,
                message_sent=False,
                event_id=prepared.event_id,
                session=prepared.session,
                detail="outbound_sender_failed_after_slot_reservation",
            )

        finalized = self.store.finalize_outbound(
            prepared.event_id,
            sent=True,
            now=self.clock() if now is None else timestamp,
        )
        if not finalized:
            return OutboundDelivery(
                outcome=ExecutionOutcome.FAILED,
                message_sent=True,
                event_id=prepared.event_id,
                session=prepared.session,
                detail="outbound_event_was_already_finalized",
            )
        return OutboundDelivery(
            outcome=ExecutionOutcome.SENT,
            message_sent=True,
            event_id=prepared.event_id,
            session=prepared.session,
            detail="outbound_message_sent",
        )


class ActionExecutor:
    """Dispatch only the four closed actions approved by the state machine."""

    def __init__(
        self,
        store: SQLiteSessionStore,
        outbound: OutboundGateway,
    ) -> None:
        self.store = store
        self.outbound = outbound

    def execute(
        self,
        customer_id: str,
        transition: StateTransition,
        *,
        reply_draft: str | None = None,
        now: float | None = None,
    ) -> ActionExecution:
        """Execute a state-machine transition, never the raw model proposal."""

        action = transition.effective_action
        if action is not None and not isinstance(action, Action):
            return self._record_rejected(
                customer_id,
                detail="effective_action_is_not_a_closed_enum_value",
                now=now,
            )

        if action is None and not self._valid_silent_transition(transition):
            return self._record_rejected(
                customer_id,
                detail="invalid_no_action_state_transition",
                now=now,
            )

        # A caller cannot smuggle a reply/schedule/close action into a state
        # that is already human-owned or closed, even if it fabricates a stale
        # StateTransition object.
        if transition.previous_state.status is not ConversationStatus.ACTIVE:
            if action is None:
                result = self.store.apply_silent_transition(
                    customer_id,
                    transition,
                    now=now,
                )
                return self._from_storage(
                    customer_id,
                    action=None,
                    result=result,
                    silent=True,
                )
            return self._record_silent(
                customer_id,
                detail="non_active_state_blocks_every_automatic_action",
                now=now,
            )

        if action is None:
            return self._from_storage(
                customer_id,
                action=None,
                result=self.store.apply_silent_transition(
                    customer_id,
                    transition,
                    now=now,
                ),
                silent=True,
            )

        if transition.next_state.revision != transition.previous_state.revision + 1:
            return self._record_rejected(
                customer_id,
                action=action,
                detail="transition_revision_is_not_next_in_sequence",
                now=now,
            )

        if action is Action.REPLY:
            if transition.event not in {
                TransitionEvent.NORMAL,
                TransitionEvent.ISSUE_RECORDED,
            } or transition.silent:
                return self._record_rejected(
                    customer_id,
                    action=action,
                    detail="reply_transition_has_invalid_event_or_silence_flag",
                    now=now,
                )
            if transition.next_state.status is not ConversationStatus.ACTIVE:
                return self._record_rejected(
                    customer_id,
                    action=action,
                    detail="reply_must_leave_conversation_active",
                    now=now,
                )
            if reply_draft is None or not reply_draft.strip():
                return self._record_rejected(
                    customer_id,
                    action=action,
                    detail="reply_requires_a_non_empty_draft",
                    now=now,
                )
            delivery = self.outbound.send(
                customer_id,
                transition,
                reply_draft,
                now=now,
            )
            return ActionExecution(
                customer_id=customer_id,
                action=action,
                outcome=delivery.outcome,
                message_sent=delivery.message_sent,
                silent=False,
                event_id=delivery.event_id,
                state=delivery.session.state,
                detail=delivery.detail,
            )

        if action is Action.SCHEDULE_FOLLOWUP:
            if transition.event not in {
                TransitionEvent.NORMAL,
                TransitionEvent.ISSUE_RECORDED,
            } or not transition.silent:
                return self._record_rejected(
                    customer_id,
                    action=action,
                    detail="schedule_transition_has_invalid_event_or_silence_flag",
                    now=now,
                )
            if transition.next_state.status is not ConversationStatus.ACTIVE:
                return self._record_rejected(
                    customer_id,
                    action=action,
                    detail="schedule_followup_must_leave_conversation_active",
                    now=now,
                )
            result = self.store.apply_non_reply_transition(
                customer_id,
                transition,
                outcome=EventOutcome.SCHEDULED,
                now=now,
            )
            return self._from_storage(
                customer_id,
                action=action,
                result=result,
                silent=True,
            )

        if action is Action.ESCALATE_TO_HUMAN:
            if transition.event not in {
                TransitionEvent.FORCED_ESCALATION,
                TransitionEvent.MODEL_ESCALATION,
            } or not transition.silent:
                return self._record_rejected(
                    customer_id,
                    action=action,
                    detail="escalation_transition_has_invalid_event_or_silence_flag",
                    now=now,
                )
            if transition.next_state.status is not ConversationStatus.HUMAN_TAKEOVER:
                return self._record_rejected(
                    customer_id,
                    action=action,
                    detail="escalation_must_enter_human_takeover",
                    now=now,
                )
            result = self.store.apply_non_reply_transition(
                customer_id,
                transition,
                outcome=EventOutcome.ESCALATED,
                now=now,
            )
            return self._from_storage(
                customer_id,
                action=action,
                result=result,
                silent=True,
            )

        if action is Action.MARK_NOT_INTERESTED:
            if (
                transition.event is not TransitionEvent.CLOSED_NOT_INTERESTED
                or not transition.silent
            ):
                return self._record_rejected(
                    customer_id,
                    action=action,
                    detail="close_transition_has_invalid_event_or_silence_flag",
                    now=now,
                )
            if transition.next_state.status is not ConversationStatus.CLOSED_NOT_INTERESTED:
                return self._record_rejected(
                    customer_id,
                    action=action,
                    detail="mark_not_interested_must_enter_closed_state",
                    now=now,
                )
            result = self.store.apply_non_reply_transition(
                customer_id,
                transition,
                outcome=EventOutcome.CLOSED,
                now=now,
            )
            return self._from_storage(
                customer_id,
                action=action,
                result=result,
                silent=True,
            )

        return self._record_rejected(
            customer_id,
            action=action,
            detail="unsupported_action",
            now=now,
        )

    def _record_silent(
        self,
        customer_id: str,
        *,
        detail: str,
        now: float | None,
    ) -> ActionExecution:
        result = self.store.record_event(
            customer_id,
            action=None,
            outcome=EventOutcome.SILENT,
            detail=detail,
            now=now,
        )
        return self._from_storage(
            customer_id,
            action=None,
            result=result,
            silent=True,
        )

    @staticmethod
    def _same_state(left: ConversationState, right: ConversationState) -> bool:
        return (
            left.status is right.status
            and left.issue_streak == right.issue_streak
            and left.revision == right.revision
        )

    @classmethod
    def _valid_silent_transition(cls, transition: StateTransition) -> bool:
        if not transition.silent:
            return False
        previous = transition.previous_state
        next_state = transition.next_state
        if transition.event is TransitionEvent.HUMAN_REACTIVATED:
            return (
                previous.status is ConversationStatus.HUMAN_TAKEOVER
                and next_state.status is ConversationStatus.ACTIVE
                and next_state.issue_streak == 0
                and next_state.revision == previous.revision + 1
            )
        if transition.event is TransitionEvent.SILENT_HUMAN_TAKEOVER:
            return (
                previous.status is ConversationStatus.HUMAN_TAKEOVER
                and cls._same_state(previous, next_state)
            )
        if transition.event is TransitionEvent.SILENT_CLOSED:
            return (
                previous.status is ConversationStatus.CLOSED_NOT_INTERESTED
                and cls._same_state(previous, next_state)
            )
        if transition.event is TransitionEvent.REACTIVATION_IGNORED:
            return (
                previous.status is ConversationStatus.ACTIVE
                and cls._same_state(previous, next_state)
            )
        if transition.event is TransitionEvent.REACTIVATION_REJECTED:
            return (
                previous.status is ConversationStatus.CLOSED_NOT_INTERESTED
                and cls._same_state(previous, next_state)
            )
        return False

    def _record_rejected(
        self,
        customer_id: str,
        *,
        action: Action | None = None,
        detail: str,
        now: float | None,
    ) -> ActionExecution:
        result = self.store.record_event(
            customer_id,
            action=None,
            outcome=EventOutcome.REJECTED,
            detail=detail,
            now=now,
        )
        return self._from_storage(
            customer_id,
            action=action,
            result=result,
            silent=True,
        )

    @staticmethod
    def _from_storage(
        customer_id: str,
        *,
        action: Action | None,
        result: StorageActionResult,
        silent: bool,
    ) -> ActionExecution:
        outcome_map = {
            EventOutcome.SCHEDULED: ExecutionOutcome.SCHEDULED,
            EventOutcome.ESCALATED: ExecutionOutcome.ESCALATED,
            EventOutcome.CLOSED: ExecutionOutcome.CLOSED,
            EventOutcome.SILENT: ExecutionOutcome.SILENT,
            EventOutcome.STALE: ExecutionOutcome.STALE,
            EventOutcome.REJECTED: ExecutionOutcome.REJECTED,
            EventOutcome.REACTIVATED: ExecutionOutcome.REACTIVATED,
        }
        try:
            outcome = outcome_map[result.outcome]
        except KeyError as exc:
            raise RuntimeError(
                f"unexpected storage outcome: {result.outcome.value}"
            ) from exc
        return ActionExecution(
            customer_id=customer_id,
            action=action,
            outcome=outcome,
            message_sent=False,
            silent=silent,
            event_id=result.event_id,
            state=result.session.state,
            detail=result.detail or result.outcome.value,
        )
