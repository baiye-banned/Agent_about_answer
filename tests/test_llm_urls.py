import asyncio

import pytest

import rag.llm as llm

from rag.llm import _message_text, deepseek_chat_url, openai_base_url, openai_chat_url, parse_json_object


def test_openai_chat_url_normalizes_v1_suffix():
    assert openai_chat_url("https://example.com/compatible-mode/v1") == "https://example.com/compatible-mode/v1/chat/completions"
    assert openai_chat_url("https://example.com/compatible-mode") == "https://example.com/compatible-mode/v1/chat/completions"


def test_openai_base_url_normalizes_v1_suffix():
    assert openai_base_url("https://example.com/compatible-mode/v1") == "https://example.com/compatible-mode/v1"
    assert openai_base_url("https://example.com/compatible-mode") == "https://example.com/compatible-mode/v1"


def test_openai_base_url_trims_surrounding_whitespace():
    assert openai_base_url("  https://example.com/compatible-mode/v1/  ") == "https://example.com/compatible-mode/v1"
    assert openai_chat_url("  https://example.com/compatible-mode  ") == "https://example.com/compatible-mode/v1/chat/completions"


def test_deepseek_chat_url_uses_configured_base():
    assert deepseek_chat_url().endswith("/chat/completions")


def test_message_text_preserves_falsy_content_parts():
    assert _message_text([{"text": 0}, {"content": False}, 0, False, None]) == "0False0False"


def test_parse_json_object_preserves_falsy_content():
    assert parse_json_object(0) == 0
    assert parse_json_object(False) is False


def test_parse_json_object_returns_error_for_non_json_text():
    result = parse_json_object("not json")

    assert result["error"]


def test_call_chat_json_rejects_non_object_json(monkeypatch):
    async def fake_call_chat_text(*args, **kwargs):
        return "[1, 2]"

    monkeypatch.setattr(llm, "call_chat_text", fake_call_chat_text)

    with pytest.raises(ValueError, match="chat model returned non-object JSON"):
        asyncio.run(llm.call_chat_json("system", "user"))


def test_call_chat_json_rejects_malformed_json_text(monkeypatch):
    async def fake_call_chat_text(*args, **kwargs):
        return "not json"

    monkeypatch.setattr(llm, "call_chat_text", fake_call_chat_text)

    with pytest.raises(ValueError, match="chat model returned invalid JSON"):
        asyncio.run(llm.call_chat_json("system", "user"))


def test_call_router_json_rejects_non_object_json(monkeypatch):
    class FakeMessage:
        def __init__(self, content):
            self.content = content

    class FakeModel:
        async def ainvoke(self, messages):
            return FakeMessage("[1, 2]")

    monkeypatch.setattr(llm, "TEXT_FALLBACK_API_KEY", "test-key")
    monkeypatch.setattr(llm, "get_text_fallback_model", lambda **kwargs: FakeModel())

    with pytest.raises(ValueError, match="router model returned non-object JSON"):
        asyncio.run(llm.call_router_json({"question": "test"}))


def test_call_router_json_rejects_malformed_json_text(monkeypatch):
    class FakeMessage:
        def __init__(self, content):
            self.content = content

    class FakeModel:
        async def ainvoke(self, messages):
            return FakeMessage("not json")

    monkeypatch.setattr(llm, "TEXT_FALLBACK_API_KEY", "test-key")
    monkeypatch.setattr(llm, "get_text_fallback_model", lambda **kwargs: FakeModel())

    with pytest.raises(ValueError, match="router model returned invalid JSON"):
        asyncio.run(llm.call_router_json({"question": "test"}))
