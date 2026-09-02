"""Concrete outbound adapter for the local simulated conversation channel."""

from dataclasses import dataclass, field
from threading import Lock


@dataclass
class InMemoryOutboundSender:
    """Record replies accepted by the gateway as simulated deliveries."""

    _deliveries: list[tuple[str, str]] = field(default_factory=list, init=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def send(self, customer_id: str, content: str) -> None:
        with self._lock:
            self._deliveries.append((customer_id, content))

    @property
    def deliveries(self) -> tuple[tuple[str, str], ...]:
        with self._lock:
            return tuple(self._deliveries)
