import pytest
from fastapi.testclient import TestClient

from api import main


def test_production_http_api_requires_desktop_token(monkeypatch):
    monkeypatch.setattr(main, "is_development", False)
    monkeypatch.setenv("CODEINSIGHT_DESKTOP_TOKEN", "desktop-secret")

    with TestClient(main.app) as client:
        assert client.get("/private-missing").status_code == 401
        assert (
            client.get(
                "/private-missing",
                headers={"X-CodeInsight-Token": "desktop-secret"},
            ).status_code
            == 404
        )


def test_production_api_fails_closed_without_configured_token(monkeypatch):
    monkeypatch.setattr(main, "is_development", False)
    monkeypatch.delenv("CODEINSIGHT_DESKTOP_TOKEN", raising=False)

    with TestClient(main.app) as client:
        response = client.get("/private-missing")

    assert response.status_code == 503


def test_health_is_single_protected_readiness_route(monkeypatch):
    monkeypatch.setattr(main, "is_development", False)
    monkeypatch.setenv("CODEINSIGHT_DESKTOP_TOKEN", "desktop-secret")

    health_routes = [route for route in main.app.routes if route.path == "/health"]
    assert len(health_routes) == 1
    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 401
        response = client.get(
            "/health", headers={"X-CodeInsight-Token": "desktop-secret"}
        )

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        ("http://tauri.localhost", True),
        ("tauri://localhost", True),
        ("https://tauri.localhost", False),
        ("https://evil.example", False),
    ],
)
def test_cors_documents_actual_tauri_production_origins(origin, allowed):
    with TestClient(main.app) as client:
        response = client.options(
            "/health",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
            },
        )

    if allowed:
        assert response.headers["access-control-allow-origin"] == origin
    else:
        assert "access-control-allow-origin" not in response.headers
