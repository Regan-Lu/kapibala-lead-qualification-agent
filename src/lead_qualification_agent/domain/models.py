from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class Intent(StrEnum):
    INTERESTED = "interested"
    NEED_MORE_INFO = "need_more_info"
    REJECTED = "rejected"
    OFF_TOPIC = "off_topic"
    OTHER = "other"


class Action(StrEnum):
    REPLY = "reply"
    SCHEDULE_FOLLOWUP = "schedule_followup"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    MARK_NOT_INTERESTED = "mark_not_interested"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    HUMAN_TAKEOVER = "human_takeover"
    CLOSED_NOT_INTERESTED = "closed_not_interested"


class MessageRole(StrEnum):
    CUSTOMER = "customer"
    AGENT = "agent"


class ReplyRisk(StrEnum):
    SAFE = "safe"
    INTERNAL_DISCLOSURE = "internal_disclosure"
    UNSUPPORTED_CLAIM = "unsupported_claim"


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class ConversationMessage(ContractModel):
    role: MessageRole
    content: str = Field(min_length=1, max_length=4_000)


class AnalyzerInput(ContractModel):
    customer_message: str = Field(min_length=1, max_length=4_000)
    history: tuple[ConversationMessage, ...] = ()


class AnalysisResult(ContractModel):
    intent: Intent
    is_dissatisfied: StrictBool
    proposed_action: Action
    reply_draft: str | None = Field(..., max_length=2_000)
    decision_note: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_reply_draft(self) -> Self:
        if self.proposed_action is Action.REPLY and not self.reply_draft:
            raise ValueError("reply_draft is required when proposed_action is reply")
        if self.proposed_action is not Action.REPLY and self.reply_draft is not None:
            raise ValueError("reply_draft is only allowed when proposed_action is reply")
        return self


class ReplyReview(ContractModel):
    allowed: StrictBool
    risk: ReplyRisk
    decision_note: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_risk_matches_decision(self) -> Self:
        if self.allowed != (self.risk is ReplyRisk.SAFE):
            raise ValueError("only a safe reply can be allowed")
        return self
