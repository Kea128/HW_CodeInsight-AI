from adalflow.core.types import ModelType

from api.clients.openai_compat import CompatibleChatClient
from api.services.oplog import clear_events_for_tests, export_text, log_event, recent_events


def test_compatible_client_uses_chat_completions_payload(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.example.com")
    client = CompatibleChatClient()
    assert str(client.base_url).rstrip("/") == "https://gateway.example.com/v1"
    kwargs = client.convert_inputs_to_api_kwargs(
        input="hello custom api",
        model_kwargs={"model": "qwen-plus", "stream": True},
        model_type=ModelType.LLM,
    )
    assert kwargs["messages"] == [{"role": "user", "content": "hello custom api"}]
    assert "input" not in kwargs
    assert kwargs["model"] == "qwen-plus"


def test_operation_log_redacts_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    clear_events_for_tests()
    log_event(
        "probe_ok",
        "连接成功",
        api_key="sk-secret",
        password="hunter2",
        model="qwen-plus",
    )
    events = recent_events()
    assert events[-1]["data"]["api_key"] == "***"
    assert events[-1]["data"]["password"] == "***"
    assert events[-1]["data"]["model"] == "qwen-plus"
    text = export_text()
    assert "sk-secret" not in text
    assert "qwen-plus" in text
    assert (tmp_path / "CodeInsight-AI" / "operation.log").is_file()
