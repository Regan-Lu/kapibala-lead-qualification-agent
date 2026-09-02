from typing import Protocol

from lead_qualification_agent.domain import ReplyReview


class ReplyGuard(Protocol):
    async def review(
        self,
        customer_message: str,
        reply_draft: str,
    ) -> ReplyReview:
        """Review a candidate customer-facing reply before it can be sent."""
        ...
