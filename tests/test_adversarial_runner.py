import asyncio
import json

import pytest

from scripts.run_adversarial_scenarios import (
    EvidenceBoundaryError,
    ModelCallCounters,
    ModelCallSnapshot,
    ObservableAnalyzer,
    ObservableReplyGuard,
    ScenarioEvidence,
    _rolling_limit_holds,
    _takeover_bypass_blocked,
    _two_issue_takeover_holds,
    project_public_turn_evidence,
    validate_public_turn_payload,
)
from lead_qualification_agent.domain import AnalyzerInput
from lead_qualification_agent.ports import ModelServiceError


def turn(**overrides):
    payload = {
        "customer_id": "phase7-test",
        "intent": "need_more_info",
        "is_dissatisfied": False,
        "action": "reply",
        "outcome": "sent",
        "message_sent": True,
        "reply": "Public product information.",
        "status": "active",
        "issue_streak": 0,
        "revision": 1,
    }
    payload.update(overrides)
    return payload


def test_public_turn_validation_accepts_only_the_api_dto() -> None:
    payload = turn()

    assert validate_public_turn_payload(payload) == payload

    unexpected = turn()
    unexpected["decision_note"] = "private-analysis-value"
    with pytest.raises(EvidenceBoundaryError) as caught:
        validate_public_turn_payload(unexpected)

    assert str(caught.value) == "public_turn_schema_mismatch"
    assert "private-analysis-value" not in str(caught.value)


def test_public_turn_validation_never_exposes_an_unsent_draft() -> None:
    payload = turn(
        outcome="rate_limited",
        message_sent=False,
        reply="candidate draft must remain internal",
    )

    with pytest.raises(EvidenceBoundaryError) as caught:
        validate_public_turn_payload(payload)

    assert str(caught.value) == "unsent_reply_was_exposed"
    assert "candidate draft" not in str(caught.value)


def test_evidence_projection_replaces_generated_reply_with_metadata() -> None:
    generated_text = "A generated response awaiting human review."

    direct_projection = project_public_turn_evidence(turn(reply=generated_text))
    evidence = ScenarioEvidence(
        name="safe-output-test",
        status="PASS",
        check="projection_applied",
        responses=(turn(reply=generated_text),),
        model_calls={
            "analysis": {"calls": 1, "failures": 0},
            "reply_review": {"calls": 1, "failures": 0},
        },
    ).as_public_dict()
    projected = evidence["responses"][0]
    rendered = json.dumps(evidence)

    assert projected == direct_projection
    assert "reply" not in projected
    assert projected["reply_present"] is True
    assert projected["reply_characters"] == len(generated_text)
    assert generated_text not in rendered


def test_model_call_deltas_publish_counts_without_failure_details() -> None:
    counters = ModelCallCounters(
        analysis_calls=4,
        analysis_failures=1,
        reply_review_calls=2,
        reply_review_failures=1,
    )

    delta = counters.snapshot().delta(ModelCallSnapshot(1, 0, 1, 0))

    assert delta == {
        "analysis": {"calls": 3, "failures": 1},
        "reply_review": {"calls": 1, "failures": 1},
    }


def test_observable_model_wrappers_count_failures_without_recording_text() -> None:
    private_diagnostic = "provider diagnostic must not be evidence"

    class FailingAnalyzer:
        async def analyze(self, request):
            del request
            raise ModelServiceError(private_diagnostic)

    class FailingReplyGuard:
        async def review(self, customer_message, reply_draft):
            del customer_message, reply_draft
            raise ModelServiceError(private_diagnostic)

    counters = ModelCallCounters()
    analyzer = ObservableAnalyzer(FailingAnalyzer(), counters)
    guard = ObservableReplyGuard(FailingReplyGuard(), counters)

    with pytest.raises(ModelServiceError):
        asyncio.run(analyzer.analyze(AnalyzerInput(customer_message="Hello")))
    with pytest.raises(ModelServiceError):
        asyncio.run(guard.review("Hello", "Public reply"))

    published = counters.snapshot().delta(ModelCallSnapshot(0, 0, 0, 0))
    rendered = json.dumps(published)
    assert published == {
        "analysis": {"calls": 1, "failures": 1},
        "reply_review": {"calls": 1, "failures": 1},
    }
    assert private_diagnostic not in rendered


def test_issue_takeover_and_bypass_checks_use_only_public_fields() -> None:
    first = turn(
        is_dissatisfied=True,
        status="active",
        issue_streak=1,
    )
    second = turn(
        intent="off_topic",
        is_dissatisfied=True,
        action="escalate_to_human",
        outcome="escalated",
        message_sent=False,
        reply=None,
        status="human_takeover",
        issue_streak=2,
        revision=2,
    )
    bypass = turn(
        intent=None,
        is_dissatisfied=None,
        action=None,
        outcome="silent",
        message_sent=False,
        reply=None,
        status="human_takeover",
        issue_streak=2,
        revision=2,
    )

    assert _two_issue_takeover_holds(first, second)
    assert _takeover_bypass_blocked(bypass)


def test_rolling_limit_check_distinguishes_59_9_from_60_seconds() -> None:
    first = turn(revision=1)
    before_boundary = turn(
        outcome="rate_limited",
        message_sent=False,
        reply=None,
        revision=2,
    )
    at_boundary = turn(revision=3)

    assert _rolling_limit_holds(first, before_boundary, at_boundary)
    assert not _rolling_limit_holds(first, at_boundary, before_boundary)
