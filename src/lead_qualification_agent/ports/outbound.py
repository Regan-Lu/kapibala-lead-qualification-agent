from typing import Protocol


class OutboundSender(Protocol):
    """The only side-effect port exposed to the reply action."""

    def send(self, customer_id: str, content: str) -> None:
        """Send one customer-visible message."""
