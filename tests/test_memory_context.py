from types import SimpleNamespace

from rag.memory_service import _build_memory_context, _group_message_texts_into_turns


def test_build_memory_context_joins_summary_and_recent_text():
    conversation = SimpleNamespace(memory_summary="长期摘要", memory_summary_upto_message_id=12)

    result = _build_memory_context(conversation, recent_text="最近对话")

    assert "长期摘要" in result
    assert "最近对话" in result


def test_build_memory_context_skips_empty_sections():
    conversation = SimpleNamespace(memory_summary="   ", memory_summary_upto_message_id=0)

    assert _build_memory_context(conversation, recent_text="") == ""


def test_group_message_texts_preserves_falsy_content():
    messages = [
        SimpleNamespace(role="user", content=0),
        SimpleNamespace(role="assistant", content=False),
    ]

    turns = _group_message_texts_into_turns(messages)

    assert len(turns) == 1
    assert "0" in turns[0][0]
    assert "False" in turns[0][1]
