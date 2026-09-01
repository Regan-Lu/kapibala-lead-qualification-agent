from collections import deque
from collections.abc import Iterable

from lead_qualification_agent.domain import AnalysisResult, AnalyzerInput


class FakeAnalyzer:
    def __init__(self, results: Iterable[AnalysisResult]) -> None:
        validated_results = list(results)
        if not all(isinstance(result, AnalysisResult) for result in validated_results):
            raise TypeError("FakeAnalyzer accepts validated AnalysisResult objects only")

        self._results = deque(validated_results)
        self.calls: list[AnalyzerInput] = []

    async def analyze(self, request: AnalyzerInput) -> AnalysisResult:
        self.calls.append(request)
        if not self._results:
            raise AssertionError("FakeAnalyzer has no configured result left")
        return self._results.popleft()
