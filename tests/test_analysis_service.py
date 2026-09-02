import asyncio
from dataclasses import dataclass

from lead_qualification_agent.application import GuardedAnalysisService
from lead_qualification_agent.domain import (
    Action,
    AnalysisResult,
    AnalyzerInput,
    Intent,
    ReplyReview,
    ReplyRisk,
)
from lead_qualification_agent.ports import ModelServiceError


def result(action: Action = Action.REPLY) -> AnalysisResult:
    return AnalysisResult(
        intent=Intent.NEED_MORE_INFO,
        is_dissatisfied=False,
        proposed_action=action,
        reply_draft="Here is public product information."
        if action is Action.REPLY
        else None,
        decision_note="test_analysis",
    )


@dataclass
class StubAnalyzer:
    output: AnalysisResult | Exception

    async def analyze(self, request: AnalyzerInput) -> AnalysisResult:
        del request
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


@dataclass
class StubGuard:
    output: ReplyReview | Exception
    calls: int = 0

    async def review(
        self,
        customer_message: str,
        reply_draft: str,
    ) -> ReplyReview:
        assert customer_message == "Hello"
        assert reply_draft
        self.calls += 1
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


def review(*, allowed: bool) -> ReplyReview:
    return ReplyReview(
        allowed=allowed,
        risk=ReplyRisk.SAFE if allowed else ReplyRisk.INTERNAL_DISCLOSURE,
        decision_note="safe" if allowed else "contains_internal_information",
    )


def run(service: GuardedAnalysisService) -> AnalysisResult:
    return asyncio.run(service.analyze(AnalyzerInput(customer_message="Hello")))


def test_safe_reply_passes_the_independent_review() -> None:
    expected = result()
    guard = StubGuard(review(allowed=True))

    returned = run(GuardedAnalysisService(StubAnalyzer(expected), guard))

    assert returned == expected
    assert guard.calls == 1


def test_blocked_reply_is_cleared_and_escalated() -> None:
    returned = run(
        GuardedAnalysisService(
            StubAnalyzer(result()),
            StubGuard(review(allowed=False)),
        )
    )

    assert returned.proposed_action is Action.ESCALATE_TO_HUMAN
    assert returned.reply_draft is None
    assert returned.decision_note == "reply_review_blocked"


def test_model_or_review_failure_schedules_without_a_reply() -> None:
    analysis_failure = run(
        GuardedAnalysisService(
            StubAnalyzer(ModelServiceError("unavailable")),
            StubGuard(review(allowed=True)),
        )
    )
    review_failure = run(
        GuardedAnalysisService(
            StubAnalyzer(result()),
            StubGuard(ModelServiceError("unavailable")),
        )
    )

    assert analysis_failure.intent is Intent.OTHER
    assert analysis_failure.proposed_action is Action.SCHEDULE_FOLLOWUP
    assert analysis_failure.reply_draft is None
    assert analysis_failure.decision_note == "analysis_unavailable"
    assert review_failure.proposed_action is Action.SCHEDULE_FOLLOWUP
    assert review_failure.reply_draft is None
    assert review_failure.decision_note == "reply_review_unavailable"


def test_non_reply_action_does_not_call_reply_guard() -> None:
    guard = StubGuard(ModelServiceError("must not be called"))
    expected = result(Action.SCHEDULE_FOLLOWUP)

    returned = run(GuardedAnalysisService(StubAnalyzer(expected), guard))

    assert returned == expected
    assert guard.calls == 0
