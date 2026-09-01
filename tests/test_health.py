import asyncio
import json
from typing import Any

from lead_qualification_agent.app import app


async def asgi_get(path: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("test-client", 123),
        "server": ("test-server", 80),
        "root_path": "",
    }
    await app(scope, receive, send)
    return messages


def test_health_endpoint() -> None:
    messages = asyncio.run(asgi_get("/health"))
    response_start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )

    assert response_start["status"] == 200
    assert json.loads(response_body) == {"status": "ok"}
