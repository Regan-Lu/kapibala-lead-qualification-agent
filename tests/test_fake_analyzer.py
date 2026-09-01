import asyncio

import pytest

from lead_qualification_agent.domain import (
    Action,
    AnalysisResult,
    AnalyzerInput,
    Intent,
)
from tests.fakes import FakeAnalyzer


def analysis(intent: Intent, action: Action) -> AnalysisResult:
    return AnalysisResult(
        intent=intent,
        is_dissatisfied=False,
        proposed_action=action,
        reply_draft="Here is the requested information."
        if action is Action.REPLY
        else None,
        decision_note="Configured deterministic test result.",
    )


def test_fake_analyzer_returns_results_in_order_and_records_calls() -> None:
    first = analysis(Intent.INTERESTED, Action.REPLY)
    second = analysis(Intent.REJECTED, Action.MARK_NOT_INTERESTED)
    analyzer = FakeAnalyzer([first, second])
    first_request = AnalyzerInput(customer_message="Tell me more")
    second_request = AnalyzerInput(customer_message="No thanks")

    async def exercise() -> tuple[AnalysisResult, AnalysisResult]:
        return (
            await analyzer.analyze(first_request),
            await analyzer.analyze(second_request),
        )

    returned = asyncio.run(exercise())

    assert returned == (first, second)
    assert analyzer.calls == [first_request, second_request]


def test_fake_analyzer_fails_when_results_are_exhausted() -> None:
    analyzer = FakeAnalyzer([])

    with pytest.raises(AssertionError, match="no configured result"):
        asyncio.run(analyzer.analyze(AnalyzerInput(customer_message="Hello")))


def test_fake_analyzer_rejects_unvalidated_dicts() -> None:
    with pytest.raises(TypeError, match="validated AnalysisResult"):
        FakeAnalyzer([{"intent": "interested"}])  # type: ignore[list-item]
