import rag.learning_trace as learning_trace
from rag.learning_trace import TraceRecorder, sanitize_trace_value, summarize_messages, summarize_text


def test_sanitize_trace_value_preserves_falsy_values():
    value = {
        "false_value": False,
        "zero_value": 0,
        "empty_list": [],
        "empty_string": "",
        "nested": {"token": "secret", "ok": False},
    }

    assert sanitize_trace_value(value) == {
        "false_value": False,
        "zero_value": 0,
        "empty_list": [],
        "empty_string": "",
        "nested": {"token": "***", "ok": False},
    }


def test_trace_recorder_only_defaults_none_fields(monkeypatch):
    monkeypatch.setattr(learning_trace, "LEARNING_TRACE_ENABLED", False)
    recorder = TraceRecorder()
    recorder.enabled = True
    recorder._safe_persist = lambda **kwargs: None

    event = recorder.add(
        "stage",
        "function",
        creates=False,
        uses=0,
        params=[],
        result=None,
    )

    assert event["creates"] is False
    assert event["uses"] == 0
    assert event["params"] == []
    assert event["result"] == {}


def test_summarize_text_preserves_falsy_values():
    assert summarize_text(0) == "0"
    assert summarize_text(False) == "False"
    assert summarize_text(None) == ""


def test_summarize_messages_preserves_falsy_content():
    class Message:
        id = 1
        role = "assistant"
        content = 0

    assert summarize_messages([Message()]) == [
        {
            "id": 1,
            "role": "assistant",
            "content": "0",
        }
    ]
