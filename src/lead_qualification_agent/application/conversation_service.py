"""Single application path from customer text to an executed safe action."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import time

from lead_qualification_agent.adapters.sqlite import (
    DemoResetResult,
    SQLiteSessionStore,
    StoredEvent,
    StoredSession,
)
from lead_qualification_agent.application.executor import (
    ActionExecution,
    ActionExecutor,
    ExecutionOutcome,
)
from lead_qualification_agent.application.analysis_service import (
    GuardedAnalysisService,
)
from lead_qualification_agent.domain import (
    Action,
    AnalysisResult,
    AnalyzerInput,
    ConversationStatus,
    handle_analysis,
    hold_inactive,
    reactivate,
)


class ModelConfigurationError(RuntimeError):
    """Raised when the customer path is used without a configured model."""


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    analysis: AnalysisResult | None
    execution: ActionExecution
    reply: str | None


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    session: StoredSession
    events: tuple[StoredEvent, ...]


class ConversationService:
    """Own the only customer-message path through analysis and execution."""

    def __init__(
        self,
        store: SQLiteSessionStore,
        analysis_service: GuardedAnalysisService | None,
        executor: ActionExecutor,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._analysis_service = analysis_service
        self._executor = executor
        self._clock = clock

    async def handle_customer_message(
        self,
        customer_id: str,
        message: str,
    ) -> ConversationTurn:
        session = await asyncio.to_thread(self._store.get_session, customer_id)
        if session is None:
            if self._analysis_service is None:
                raise ModelConfigurationError(
                    "the model service is not configured"
                )
            session = await asyncio.to_thread(
                self._store.ensure_session,
                customer_id,
                now=self._clock(),
            )

        if session.state.status is not ConversationStatus.ACTIVE:
            execution = await asyncio.to_thread(
                self._executor.execute,
                customer_id,
                hold_inactive(session.state),
                now=self._clock(),
            )
            return ConversationTurn(
                analysis=None,
                execution=execution,
                reply=None,
            )

        if self._analysis_service is None:
            raise ModelConfigurationError("the model service is not configured")

        analysis = await self._analysis_service.analyze(
            AnalyzerInput(customer_message=message)
        )
        transition = handle_analysis(session.state, analysis)
        execution = await asyncio.to_thread(
            self._executor.execute,
            customer_id,
            transition,
            reply_draft=analysis.reply_draft,
            now=self._clock(),
        )
        reply = (
            analysis.reply_draft
            if execution.action is Action.REPLY
            and execution.outcome is ExecutionOutcome.SENT
            and execution.message_sent
            else None
        )
        return ConversationTurn(
            analysis=analysis,
            execution=execution,
            reply=reply,
        )

    async def get_snapshot(
        self,
        customer_id: str,
        *,
        event_limit: int = 50,
    ) -> ConversationSnapshot | None:
        snapshot = await asyncio.to_thread(
            self._store.get_snapshot,
            customer_id,
            event_limit=event_limit,
        )
        if snapshot is None:
            return None
        session, events = snapshot
        return ConversationSnapshot(session=session, events=events)

    async def reactivate(self, customer_id: str) -> ActionExecution | None:
        session = await asyncio.to_thread(self._store.get_session, customer_id)
        if session is None:
            return None
        return await asyncio.to_thread(
            self._executor.execute,
            customer_id,
            reactivate(session.state),
            now=self._clock(),
        )

    async def reset_demo(self) -> DemoResetResult:
        return await asyncio.to_thread(self._store.reset_demo)
