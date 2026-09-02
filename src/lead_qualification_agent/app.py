"""FastAPI composition root and deliberately small public API."""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Response, status
from pydantic import BaseModel, ConfigDict, Field

from lead_qualification_agent.adapters import (
    GeminiAnalyzer,
    GeminiInteractionClient,
    GeminiReplyGuard,
    GeminiSettings,
    InMemoryOutboundSender,
    SQLiteSessionStore,
)
from lead_qualification_agent.application import (
    ActionExecutor,
    ConversationService,
    ConversationSnapshot,
    ConversationTurn,
    ExecutionOutcome,
    GuardedAnalysisService,
    ModelConfigurationError,
    OutboundGateway,
)
from lead_qualification_agent.domain import Action, ConversationStatus, Intent


DEFAULT_DATABASE_PATH = "lead_qualification_agent.db"
OPERATOR_TOKEN_HEADER = "X-Operator-Token"
CustomerId = Annotated[
    str,
    Path(min_length=1, max_length=128, pattern=r".*\S.*"),
]


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CustomerMessageRequest(ApiModel):
    message: str = Field(min_length=1, max_length=4_000)


class ConversationTurnResponse(ApiModel):
    customer_id: str
    intent: Intent | None
    is_dissatisfied: bool | None
    action: Action | None
    outcome: ExecutionOutcome
    message_sent: bool
    reply: str | None
    status: ConversationStatus
    issue_streak: int
    revision: int


class ConversationEventResponse(ApiModel):
    event_id: int
    action: Action | None
    outcome: str
    occurred_at: float


class ConversationSnapshotResponse(ApiModel):
    customer_id: str
    status: ConversationStatus
    issue_streak: int
    revision: int
    events: tuple[ConversationEventResponse, ...]


class OperatorActionResponse(ApiModel):
    customer_id: str
    outcome: ExecutionOutcome
    status: ConversationStatus
    issue_streak: int
    revision: int


class DemoResetResponse(ApiModel):
    sessions_deleted: int
    events_deleted: int


def _build_default_service() -> ConversationService:
    store = SQLiteSessionStore(
        os.getenv("LEAD_AGENT_DB_PATH", DEFAULT_DATABASE_PATH)
    )
    sender = InMemoryOutboundSender()
    executor = ActionExecutor(
        store,
        OutboundGateway(store, sender),
    )

    analysis_service: GuardedAnalysisService | None = None
    try:
        settings = GeminiSettings.from_env()
    except ValueError:
        pass
    else:
        client = GeminiInteractionClient(settings)
        analysis_service = GuardedAnalysisService(
            GeminiAnalyzer(client),
            GeminiReplyGuard(client),
        )
    return ConversationService(store, analysis_service, executor)


def _turn_response(turn: ConversationTurn) -> ConversationTurnResponse:
    analysis = turn.analysis
    action = (
        None
        if turn.execution.outcome is ExecutionOutcome.STALE
        else turn.execution.action
    )
    return ConversationTurnResponse(
        customer_id=turn.execution.customer_id,
        intent=None if analysis is None else analysis.intent,
        is_dissatisfied=(
            None if analysis is None else analysis.is_dissatisfied
        ),
        action=action,
        outcome=turn.execution.outcome,
        message_sent=turn.execution.message_sent,
        reply=turn.reply,
        status=turn.execution.state.status,
        issue_streak=turn.execution.state.issue_streak,
        revision=turn.execution.state.revision,
    )


def _snapshot_response(
    snapshot: ConversationSnapshot,
) -> ConversationSnapshotResponse:
    return ConversationSnapshotResponse(
        customer_id=snapshot.session.customer_id,
        status=snapshot.session.state.status,
        issue_streak=snapshot.session.state.issue_streak,
        revision=snapshot.session.state.revision,
        events=tuple(
            ConversationEventResponse(
                event_id=event.event_id,
                action=event.action,
                outcome=event.outcome.value,
                occurred_at=event.occurred_at,
            )
            for event in snapshot.events
        ),
    )


def create_app(
    service: ConversationService | None = None,
    *,
    operator_token: str | None = None,
) -> FastAPI:
    api = FastAPI(
        title="KapibalaAI Lead Qualification Agent",
        version="0.1.0",
    )
    configured_service = service
    configured_operator_token = (
        os.getenv("OPERATOR_TOKEN", "").strip()
        if operator_token is None
        else operator_token.strip()
    )

    def resolve_service() -> ConversationService:
        nonlocal configured_service
        if configured_service is None:
            configured_service = _build_default_service()
        return configured_service

    def require_operator(
        supplied_token: Annotated[
            str | None,
            Header(alias=OPERATOR_TOKEN_HEADER),
        ] = None,
    ) -> None:
        if not configured_operator_token:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "operator_controls_unavailable",
                    "message": "operator controls are not configured",
                },
            )
        if supplied_token != configured_operator_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "code": "operator_authentication_failed",
                    "message": "a valid operator token is required",
                },
            )

    @api.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.post(
        "/conversations/{customer_id}/messages",
        response_model=ConversationTurnResponse,
        tags=["conversations"],
    )
    async def handle_customer_message(
        customer_id: CustomerId,
        request: CustomerMessageRequest,
        response: Response,
    ) -> ConversationTurnResponse:
        try:
            turn = await resolve_service().handle_customer_message(
                customer_id,
                request.message,
            )
        except ModelConfigurationError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "model_unavailable",
                    "message": "the model service is not configured",
                },
            ) from None
        if turn.execution.outcome is ExecutionOutcome.STALE:
            response.status_code = status.HTTP_409_CONFLICT
        return _turn_response(turn)

    @api.get(
        "/conversations/{customer_id}",
        response_model=ConversationSnapshotResponse,
        tags=["conversations"],
    )
    async def get_conversation(
        customer_id: CustomerId,
        event_limit: int = 50,
    ) -> ConversationSnapshotResponse:
        if not 1 <= event_limit <= 200:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="event_limit must be between 1 and 200",
            )
        snapshot = await resolve_service().get_snapshot(
            customer_id,
            event_limit=event_limit,
        )
        if snapshot is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "conversation_not_found",
                    "message": "conversation does not exist",
                },
            )
        return _snapshot_response(snapshot)

    @api.post(
        "/operator/conversations/{customer_id}/reactivate",
        response_model=OperatorActionResponse,
        dependencies=[Depends(require_operator)],
        tags=["operator"],
    )
    async def reactivate_conversation(
        customer_id: CustomerId,
        response: Response,
    ) -> OperatorActionResponse:
        execution = await resolve_service().reactivate(customer_id)
        if execution is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "conversation_not_found",
                    "message": "conversation does not exist",
                },
            )
        if execution.outcome is ExecutionOutcome.STALE:
            response.status_code = status.HTTP_409_CONFLICT
        return OperatorActionResponse(
            customer_id=execution.customer_id,
            outcome=execution.outcome,
            status=execution.state.status,
            issue_streak=execution.state.issue_streak,
            revision=execution.state.revision,
        )

    @api.post(
        "/operator/demo/reset",
        response_model=DemoResetResponse,
        dependencies=[Depends(require_operator)],
        tags=["operator"],
    )
    async def reset_demo() -> DemoResetResponse:
        result = await resolve_service().reset_demo()
        return DemoResetResponse(
            sessions_deleted=result.sessions_deleted,
            events_deleted=result.events_deleted,
        )

    return api


app = create_app()
