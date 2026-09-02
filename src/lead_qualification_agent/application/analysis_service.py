"""Non-disclosing orchestration around model analysis and reply review."""

from lead_qualification_agent.domain import (
    Action,
    AnalysisResult,
    AnalyzerInput,
    Intent,
)
from lead_qualification_agent.ports import Analyzer, ModelServiceError, ReplyGuard


class GuardedAnalysisService:
    """Expose only analysis results whose reply draft passed a second review."""

    def __init__(self, analyzer: Analyzer, reply_guard: ReplyGuard) -> None:
        self._analyzer = analyzer
        self._reply_guard = reply_guard

    async def analyze(self, request: AnalyzerInput) -> AnalysisResult:
        try:
            result = await self._analyzer.analyze(request)
        except ModelServiceError:
            return self._fallback(
                intent=Intent.OTHER,
                is_dissatisfied=False,
                action=Action.SCHEDULE_FOLLOWUP,
                note="analysis_unavailable",
            )

        if result.proposed_action is not Action.REPLY:
            return result

        if result.reply_draft is None:
            raise RuntimeError("validated reply analysis lost its reply draft")

        try:
            review = await self._reply_guard.review(
                request.customer_message,
                result.reply_draft,
            )
        except ModelServiceError:
            return self._fallback(
                intent=result.intent,
                is_dissatisfied=result.is_dissatisfied,
                action=Action.SCHEDULE_FOLLOWUP,
                note="reply_review_unavailable",
            )

        if not review.allowed:
            return self._fallback(
                intent=result.intent,
                is_dissatisfied=result.is_dissatisfied,
                action=Action.ESCALATE_TO_HUMAN,
                note="reply_review_blocked",
            )
        return result

    @staticmethod
    def _fallback(
        *,
        intent: Intent,
        is_dissatisfied: bool,
        action: Action,
        note: str,
    ) -> AnalysisResult:
        return AnalysisResult(
            intent=intent,
            is_dissatisfied=is_dissatisfied,
            proposed_action=action,
            reply_draft=None,
            decision_note=note,
        )
