"""Run one real Gemini analysis and, when needed, one reply review."""

import asyncio
from collections.abc import Mapping
import json
from typing import Any

from lead_qualification_agent.adapters import (
    GeminiAnalyzer,
    GeminiInteractionClient,
    GeminiReplyGuard,
    GeminiSettings,
)
from lead_qualification_agent.application import GuardedAnalysisService
from lead_qualification_agent.domain import Action, AnalyzerInput


class CountingClient:
    def __init__(self, delegate: GeminiInteractionClient) -> None:
        self._delegate = delegate
        self.calls = 0

    async def generate_json(
        self,
        *,
        system_instruction: str,
        user_input: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        self.calls += 1
        return await self._delegate.generate_json(
            system_instruction=system_instruction,
            user_input=user_input,
            response_schema=response_schema,
        )


async def main() -> int:
    settings = GeminiSettings.from_env()
    client = CountingClient(GeminiInteractionClient(settings))
    analyzer = GeminiAnalyzer(client)
    guard = GeminiReplyGuard(client)
    service = GuardedAnalysisService(analyzer, guard)
    result = await service.analyze(
        AnalyzerInput(
            customer_message=(
                "I am interested in an AI lead-qualification workflow. "
                "What public capabilities can you explain?"
            )
        )
    )
    if (
        client.calls != 2
        or result.proposed_action is not Action.REPLY
        or result.reply_draft is None
    ):
        print(
            json.dumps(
                {
                    "smoke": "failed",
                    "model_calls": client.calls,
                    "proposed_action": result.proposed_action,
                    "decision_note": result.decision_note,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    print(
        json.dumps(
            {"guarded_analysis": result.model_dump(mode="json")},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
