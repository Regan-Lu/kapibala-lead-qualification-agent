from lead_qualification_agent.app import app, health


def test_health_endpoint() -> None:
    route_paths = {route.path for route in app.routes}

    assert "/health" in route_paths
    assert health() == {"status": "ok"}
