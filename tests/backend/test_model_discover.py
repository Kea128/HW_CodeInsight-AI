import pytest

from api.services.model_discover import (
    discover_openai_models,
    normalize_openai_base_url,
    probe_openai_chat,
)


def test_normalize_openai_base_url_appends_v1():
    assert normalize_openai_base_url("https://api.deepseek.com/") == (
        "https://api.deepseek.com/v1"
    )
    assert normalize_openai_base_url("https://api.deepseek.com/v1") == (
        "https://api.deepseek.com/v1"
    )
    assert normalize_openai_base_url(
        "[https://ai.example.com/agent/v1](https://ai.example.com/agent/v1)"
    ) == "https://ai.example.com/agent/v1"


@pytest.mark.asyncio
async def test_discover_openai_models_parses_ids(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, url, headers=None):
            assert url.endswith("/v1/models")
            assert headers["Authorization"] == "Bearer sk"
            return FakeResponse()

    monkeypatch.setattr(
        "api.services.model_discover.httpx.AsyncClient",
        lambda **_kwargs: FakeClient(),
    )
    models = await discover_openai_models("https://api.deepseek.com", "sk")
    assert [item["id"] for item in models] == ["deepseek-chat", "deepseek-reasoner"]


@pytest.mark.asyncio
async def test_test_openai_chat_posts_completion(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url, headers=None, json=None):
            assert url.endswith("/v1/chat/completions")
            assert json["model"] == "deepseek-chat"
            return FakeResponse()

    monkeypatch.setattr(
        "api.services.model_discover.httpx.AsyncClient",
        lambda **_kwargs: FakeClient(),
    )
    result = await probe_openai_chat(
        "https://api.deepseek.com", "sk", "deepseek-chat"
    )
    assert result["ok"] == "true"
