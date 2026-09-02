from fastapi.testclient import TestClient

from lead_qualification_agent.adapters.gemini import (
    ANALYSIS_SYSTEM_INSTRUCTION,
    REPLY_REVIEW_SYSTEM_INSTRUCTION,
)
from lead_qualification_agent.app import create_app
from tests.test_api import analysis, build_harness


def test_demo_page_serves_assets_without_exposing_server_configuration(
    monkeypatch,
) -> None:
    gemini_token = "ui-test-gemini-token-sentinel"
    operator_token = "ui-test-operator-token-sentinel"
    monkeypatch.setenv("GEMINI_API_KEY", gemini_token)
    client = TestClient(create_app(operator_token=operator_token))

    page = client.get("/")
    stylesheet = client.get("/static/styles.css")
    script = client.get("/static/app.js")

    assert page.status_code == 200
    assert stylesheet.status_code == 200
    assert script.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert script.headers["content-type"].startswith("text/javascript")
    assert 'href="/static/styles.css"' in page.text
    assert 'src="/static/app.js"' in page.text

    public_assets = "\n".join((page.text, stylesheet.text, script.text))
    assert gemini_token not in public_assets
    assert operator_token not in public_assets
    assert ANALYSIS_SYSTEM_INSTRUCTION not in public_assets
    assert REPLY_REVIEW_SYSTEM_INSTRUCTION not in public_assets
    assert "reply_draft" not in script.text
    assert "decision_note" not in script.text
    assert "localStorage" not in script.text
    assert "sessionStorage" not in script.text


def test_static_mount_preserves_api_and_unsent_draft_stays_server_side(
    tmp_path,
) -> None:
    first_reply = "A customer-visible answer."
    unsent_marker = "UI_TEST_UNSENT_DRAFT_MUST_STAY_PRIVATE"
    harness = build_harness(
        tmp_path,
        [
            analysis(reply_draft=first_reply),
            analysis(reply_draft=unsent_marker),
        ],
    )

    health = harness.client.get("/health")
    first = harness.client.post(
        "/conversations/ui-customer/messages",
        json={"message": "Tell me about the product."},
    )
    harness.clock.value = 1_059.9
    limited = harness.client.post(
        "/conversations/ui-customer/messages",
        json={"message": "Tell me more."},
    )
    snapshot = harness.client.get("/conversations/ui-customer")
    public_assets = [
        harness.client.get("/"),
        harness.client.get("/static/styles.css"),
        harness.client.get("/static/app.js"),
    ]

    assert health.status_code == 200
    assert health.headers["content-type"].startswith("application/json")
    assert health.json() == {"status": "ok"}
    assert first.status_code == 200
    assert first.json()["outcome"] == "sent"
    assert first.json()["reply"] == first_reply
    assert limited.status_code == 200
    assert limited.json()["outcome"] == "rate_limited"
    assert limited.json()["message_sent"] is False
    assert limited.json()["reply"] is None
    assert snapshot.status_code == 200
    assert snapshot.headers["content-type"].startswith("application/json")
    assert [event["outcome"] for event in snapshot.json()["events"]] == [
        "sent",
        "rate_limited",
    ]
    assert all(response.status_code == 200 for response in public_assets)

    public_output = "\n".join(
        [limited.text, snapshot.text]
        + [response.text for response in public_assets]
    )
    assert unsent_marker not in public_output
