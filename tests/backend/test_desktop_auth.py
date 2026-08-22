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
