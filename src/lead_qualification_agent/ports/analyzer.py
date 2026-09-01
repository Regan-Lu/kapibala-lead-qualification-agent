from typing import Protocol

from lead_qualification_agent.domain import AnalysisResult, AnalyzerInput


class Analyzer(Protocol):
    async def analyze(self, request: AnalyzerInput) -> AnalysisResult:
        """Analyze untrusted customer text and return a validated proposal."""
        ...
