import pytest
from pydantic import ValidationError

from lead_qualification_agent.domain import (
    Action,
    AnalysisResult,
    AnalyzerInput,
    ConversationMessage,
    ConversationStatus,
    Intent,
    MessageRole,
    ReplyReview,
    ReplyRisk,
)


def valid_reply_payload() -> dict[str, object]:
    return {
        "intent": "interested",
        "is_dissatisfied": False,
        "proposed_action": "reply",
        "reply_draft": "Thanks for your interest. What would you like to know?",
        "decision_note": "The customer asked a product-related question.",
    }


def test_enum_values_match_the_business_contract() -> None:
    assert {item.value for item in Intent} == {
        "interested",
        "need_more_info",
        "rejected",
        "off_topic",
        "other",
    }
    assert {item.value for item in Action} == {
        "reply",
        "schedule_followup",
        "escalate_to_human",
        "mark_not_interested",
    }
    assert {item.value for item in ConversationStatus} == {
        "active",
        "human_takeover",
        "closed_not_interested",
    }


def test_valid_json_round_trip_uses_public_string_values() -> None:
    result = AnalysisResult.model_validate_json(
        """
        {
          "intent": "interested",
          "is_dissatisfied": false,
          "proposed_action": "reply",
          "reply_draft": "I can share more details.",
          "decision_note": "The customer requested information."
        }
        """
    )

    assert result.intent is Intent.INTERESTED
    assert result.proposed_action is Action.REPLY
    assert result.model_dump(mode="json") == {
        "intent": "interested",
        "is_dissatisfied": False,
        "proposed_action": "reply",
        "reply_draft": "I can share more details.",
        "decision_note": "The customer requested information.",
    }


def test_json_schema_requires_every_structured_output_field() -> None:
    schema = AnalysisResult.model_json_schema()

    assert set(schema["required"]) == {
        "intent",
        "is_dissatisfied",
        "proposed_action",
        "reply_draft",
        "decision_note",
    }


def test_dissatisfaction_is_independent_from_intent() -> None:
    payload = valid_reply_payload()
    payload["is_dissatisfied"] = True

    result = AnalysisResult.model_validate(payload)

    assert result.intent is Intent.INTERESTED
    assert result.is_dissatisfied is True


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("intent", "delete_customer"),
        ("proposed_action", "run_shell_command"),
    ],
)
def test_unknown_intent_or_action_is_rejected(field: str, invalid_value: str) -> None:
    payload = valid_reply_payload()
    payload[field] = invalid_value

    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    ["intent", "is_dissatisfied", "proposed_action", "reply_draft", "decision_note"],
)
def test_required_fields_are_rejected_when_missing(field: str) -> None:
    payload = valid_reply_payload()
    payload.pop(field)

    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(payload)


def test_unexpected_tool_field_is_rejected() -> None:
    payload = valid_reply_payload()
    payload["tool_name"] = "delete_all_data"

    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(payload)


@pytest.mark.parametrize("invalid_value", ["false", "true", 0, 1])
def test_dissatisfaction_requires_a_real_boolean(invalid_value: object) -> None:
    payload = valid_reply_payload()
    payload["is_dissatisfied"] = invalid_value

    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(payload)


@pytest.mark.parametrize("invalid_draft", [None, "", "   "])
def test_reply_requires_a_non_empty_draft(invalid_draft: str | None) -> None:
    payload = valid_reply_payload()
    payload["reply_draft"] = invalid_draft

    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(payload)


@pytest.mark.parametrize(
    "action",
    [
        "schedule_followup",
        "escalate_to_human",
        "mark_not_interested",
    ],
)
def test_non_reply_action_rejects_a_draft(action: str) -> None:
    payload = valid_reply_payload()
    payload["proposed_action"] = action

    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(payload)


def test_non_reply_action_accepts_explicit_null_draft() -> None:
    payload = valid_reply_payload()
    payload["proposed_action"] = "schedule_followup"
    payload["reply_draft"] = None

    result = AnalysisResult.model_validate(payload)

    assert result.proposed_action is Action.SCHEDULE_FOLLOWUP
    assert result.reply_draft is None


def test_non_reply_action_requires_explicit_null_draft() -> None:
    payload = valid_reply_payload()
    payload["proposed_action"] = "schedule_followup"
    payload.pop("reply_draft")

    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(payload)


def test_analyzer_input_is_immutable_and_strips_customer_text() -> None:
    request = AnalyzerInput(
        customer_message="  Please tell me more.  ",
        history=(
            ConversationMessage(
                role=MessageRole.AGENT,
                content="Hello, how can I help?",
            ),
        ),
    )

    assert request.customer_message == "Please tell me more."
    with pytest.raises(ValidationError):
        request.customer_message = "mutated"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("allowed", "risk"),
    [
        (True, ReplyRisk.INTERNAL_DISCLOSURE),
        (False, ReplyRisk.SAFE),
    ],
)
def test_reply_review_decision_must_match_risk(
    allowed: bool,
    risk: ReplyRisk,
) -> None:
    with pytest.raises(ValidationError, match="only a safe reply"):
        ReplyReview(
            allowed=allowed,
            risk=risk,
            decision_note="inconsistent_review",
        )
