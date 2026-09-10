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
    monkeypatch.setenv("CODEINSIGHT_DESKTOP_VERSION", "9.8.7")

    health_routes = [
        route for route in main.app.routes if getattr(route, "path", None) == "/health"
    ]
    assert len(health_routes) == 1
    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 401
        response = client.get(
            "/health", headers={"X-CodeInsight-Token": "desktop-secret"}
        )

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert response.json()["engine_version"] == "9.8.7"


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


def test_detect_remote_scopes_routes_scan_local_copy(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "is_development", True)
    monkeypatch.delenv("CODEINSIGHT_DESKTOP_TOKEN", raising=False)
    from api.routers import remote

    (tmp_path / "YinWang" / "br_feature_ADS_truck_0820").mkdir(parents=True)
    project = {
        "id": "remote-abc123",
        "remote_path": "/home/WorkSpace",
        "local_path": str(tmp_path),
    }

    class FakeManager:
        store = type("Store", (), {"get_remote_project": staticmethod(lambda pid: project if pid == "remote-abc123" else None)})()

        def detect_scopes(self, project_id):
            from api.services.knowledge.spaces import list_child_candidates

            assert project_id == "remote-abc123"
            return list_child_candidates(tmp_path)

    monkeypatch.setattr(remote, "manager", FakeManager())

    with TestClient(main.app) as client:
        for method, path in (
            ("POST", "/remote/projects/remote-abc123/scopes/detect"),
            ("GET", "/remote/projects/remote-abc123/scopes/detect"),
            ("POST", "/remote/projects/remote-abc123/detect-scopes"),
        ):
            response = client.request(method, path)
            assert response.status_code == 200, f"{method} {path}: {response.text}"
            paths = {item["path"] for item in response.json()["candidates"]}
            assert "YinWang" in paths


def test_desktop_logs_routes_return_file_backed_events(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "is_development", False)
    monkeypatch.setenv("CODEINSIGHT_DESKTOP_TOKEN", "desktop-secret")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    from api.services.oplog import clear_events_for_tests, log_event

    clear_events_for_tests()
    log_event("sync_ok", "同步完成", host="10.39.48.26")

    with TestClient(main.app) as client:
        headers = {"X-CodeInsight-Token": "desktop-secret"}
        for path in ("/desktop/logs", "/desktop/operation-log"):
            response = client.get(f"{path}?limit=50", headers=headers)
            assert response.status_code == 200, path
            body = response.json()
            assert "同步完成" in body["text"]
            assert body["events"][-1]["event"] == "sync_ok"
            assert str(tmp_path / "CodeInsight-AI" / "operation.log") == body["path"]
