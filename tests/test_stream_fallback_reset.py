"""回归（issue #21）：流式中途失败切换后备模型时，必须作废已输出内容，避免重复拼接与落库。

打桩方式：替换 `rag.llm._stream_model_chunks`，第一次调用（DeepSeek）先吐两段内容再断开，
第二次调用（文本后备模型）从头输出完整回答。
"""

import asyncio
import json

import rag.llm as llm
from schema.schemas import ChatRequest
from service import chat_service


DEEPSEEK_CHUNKS = ["根据《员工手册》考勤管理", "，迟到30分钟以内"]
FALLBACK_CHUNKS = ["根据《员工手册》考勤管理章节，", "迟到30分钟以内罚款50元。", "（以上为完整回答）"]
FALLBACK_TEXT = "".join(FALLBACK_CHUNKS)


def _stub_fallback_stream(monkeypatch, *, deepseek_chunks, fallback_chunks, failure=None):
    """第一次调用按 deepseek_chunks 输出后（可选）抛异常，第二次调用输出 fallback_chunks。"""
    calls = {"count": 0}

    async def fake_stream(model, messages):
        calls["count"] += 1
        if calls["count"] == 1:
            for piece in deepseek_chunks:
                yield piece
            if failure is not None:
                raise failure
            return
        for piece in fallback_chunks:
            yield piece

    monkeypatch.setattr(llm, "_stream_model_chunks", fake_stream)
    monkeypatch.setattr(llm, "DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "TEXT_FALLBACK_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "TEXT_FALLBACK_ENABLED", True)
    return calls


def _collect_events(*args, **kwargs):
    async def _run():
        return [event async for event in llm.stream_answer_events(*args, **kwargs)]

    return asyncio.run(_run())


def _render_like_frontend(events):
    """按前端协议渲染事件流：reset 清空缓冲，content 追加，error 直接失败。"""
    rendered = ""
    resets = 0
    for event in events:
        if isinstance(event, dict):
            if event.get("type") == "reset":
                rendered = ""
                resets += 1
                continue
            if event.get("type") == "error":
                raise AssertionError(f"unexpected error event: {event.get('message')}")
            chunk = event.get("content", "")
        else:
            chunk = event
        rendered += chunk
    return rendered, resets


def _chunk_text(event):
    return event.get("content", "") if isinstance(event, dict) else event


def _reset_index(events):
    return next(
        index
        for index, event in enumerate(events)
        if isinstance(event, dict) and event.get("type") == "reset"
    )


def test_mid_stream_failure_emits_reset_before_fallback_content(monkeypatch):
    calls = _stub_fallback_stream(
        monkeypatch,
        deepseek_chunks=DEEPSEEK_CHUNKS,
        fallback_chunks=FALLBACK_CHUNKS,
        failure=RuntimeError("peer closed connection without sending complete message body"),
    )

    events = _collect_events("迟到30分钟以内怎么罚款？", "上下文", memory_context="")

    assert calls["count"] == 2
    rendered, resets = _render_like_frontend(events)
    assert resets == 1
    assert rendered == FALLBACK_TEXT
    assert "根据《员工手册》考勤管理，迟到30分钟以内" not in rendered

    index = _reset_index(events)
    assert _chunk_text(events[index - 1]) == DEEPSEEK_CHUNKS[-1]
    assert _chunk_text(events[index + 1]) == FALLBACK_CHUNKS[0]
    assert events[index].get("reason") == "text_fallback"


def test_successful_deepseek_stream_does_not_emit_reset(monkeypatch):
    _stub_fallback_stream(
        monkeypatch,
        deepseek_chunks=["完整回答"],
        fallback_chunks=[],
    )

    events = _collect_events("问题", "上下文", memory_context="")

    assert not [event for event in events if isinstance(event, dict) and event.get("type") == "reset"]
    rendered, resets = _render_like_frontend(events)
    assert resets == 0
    assert rendered == "完整回答"


def test_fallback_disabled_still_reports_error_without_reset(monkeypatch):
    _stub_fallback_stream(
        monkeypatch,
        deepseek_chunks=DEEPSEEK_CHUNKS,
        fallback_chunks=[],
        failure=RuntimeError("connection reset"),
    )
    monkeypatch.setattr(llm, "TEXT_FALLBACK_ENABLED", False)

    events = _collect_events("问题", "上下文", memory_context="")

    assert [event for event in events if isinstance(event, dict) and event.get("type") == "error"]
    assert not [event for event in events if isinstance(event, dict) and event.get("type") == "reset"]


class _FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter_by(self, **_kwargs):
        return self

    def first(self):
        return self.rows[0] if self.rows else None


class _FakeDb:
    def __init__(self, user):
        self.user = user
        self.added = []

    def query(self, model):
        if model is chat_service.User:
            return _FakeQuery([self.user])
        return _FakeQuery([])

    def add(self, item):
        self.added.append(item)

    def commit(self):
        pass

    def refresh(self, item):
        if getattr(item, "id", None) is None:
            item.id = 1

    def rollback(self):
        pass

    def close(self):
        pass


class _FakeUser:
    id = 7
    username = "alice"


class _FakeKnowledgeBase:
    id = 3
    name = "制度库"


class _FakeTrace:
    def __init__(self, user_id=None):
        self.user_id = user_id
        self.trace_id = "trace-test"
        self.events = []

    def add(self, event, owner, **payload):
        self.events.append({"event": event, "owner": owner, **payload})

    def attach(self, **payload):
        self.attached = payload

    def finish(self, status):
        self.status = status

    def snapshot(self):
        return {"trace_id": self.trace_id, "events": self.events}


def _patch_chat_service_dependencies(monkeypatch, fake_db):
    monkeypatch.setattr(chat_service, "decode_token", lambda authorization: "alice")
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: fake_db)
    monkeypatch.setattr(chat_service, "TraceRecorder", _FakeTrace)
    monkeypatch.setattr(chat_service, "resolve_knowledge_base", lambda db, kid, user_id: _FakeKnowledgeBase())

    async def fake_build_effective_question(question, attachments):
        return question, {"status": "skipped"}

    async def fake_recent_memory_text(*_args, **_kwargs):
        return ""

    async def fake_decide_need_rag(*_args, **_kwargs):
        return {"need_rag": True, "route": "rag", "confidence": 1.0, "source": "test", "reason": "needs retrieval"}

    async def fake_retrieve_knowledge(question, knowledge_base_id, db, trace_recorder=None):
        return (
            [{"file_name": "制度.txt", "content": "迟到规则", "file_id": 1, "chunk_id": "a"}],
            {"query_plan": {}, "routes": [], "rrf": [], "rerank": {"status": "done", "items": []}},
        )

    monkeypatch.setattr(chat_service, "_build_effective_question", fake_build_effective_question)
    monkeypatch.setattr(chat_service, "_build_recent_memory_text", fake_recent_memory_text)
    monkeypatch.setattr(chat_service, "_build_memory_context", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        chat_service,
        "_build_memory_aware_retrieval_question",
        lambda question, memory_context: question,
    )
    monkeypatch.setattr(chat_service, "decide_need_rag", fake_decide_need_rag)
    monkeypatch.setattr(chat_service, "retrieve_knowledge", fake_retrieve_knowledge)
    monkeypatch.setattr(chat_service, "_trace_sse_payloads", lambda trace: [])
    monkeypatch.setattr(chat_service, "_build_sources", lambda chunks: [{"file": chunks[0]["file_name"]}])
    monkeypatch.setattr(chat_service, "_attach_grounding_trace", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "_safe_trace_attach", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "_safe_trace_finish", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "_schedule_memory_summary_update", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "schedule_ragas_evaluation", lambda *args, **kwargs: None)


async def _collect_stream(iterator):
    chunks = []
    async for chunk in iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return "".join(chunks)


def test_chat_service_persists_only_content_after_reset(monkeypatch):
    fake_db = _FakeDb(_FakeUser())
    _patch_chat_service_dependencies(monkeypatch, fake_db)
    _stub_fallback_stream(
        monkeypatch,
        deepseek_chunks=DEEPSEEK_CHUNKS,
        fallback_chunks=FALLBACK_CHUNKS,
        failure=RuntimeError("peer closed connection without sending complete message body"),
    )

    async def real_chain(question, context, memory_context, trace, use_rag=True):
        async for event in llm.stream_answer_events(question, context, memory_context, trace, use_rag):
            yield event

    monkeypatch.setattr(chat_service, "stream_rag_answer", real_chain)

    response = asyncio.run(
        chat_service.stream_chat(ChatRequest(question="迟到30分钟以内怎么罚款？"), authorization="Bearer token")
    )
    body = asyncio.run(_collect_stream(response.body_iterator))

    resets = [line for line in body.split("\n\n") if '"type": "reset"' in line]
    assert len(resets) == 1

    saved = [item for item in fake_db.added if getattr(item, "role", "") == "assistant"]
    assert len(saved) == 1
    assert saved[0].content == FALLBACK_TEXT
    assert "根据《员工手册》考勤管理，迟到30分钟以内根据《员工手册》考勤管理章节，" not in saved[0].content

    # 前端按同一份事件流渲染出的最终文本必须与落库内容一致
    rendered, frontend_resets = _render_like_frontend(_events_from_sse(body))
    assert frontend_resets == 1
    assert rendered == saved[0].content


def _events_from_sse(body):
    events = []
    for line in body.split("\n\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if payload == "[DONE]":
            continue
        parsed = json.loads(payload)
        events.append(parsed if "type" in parsed else parsed.get("content", ""))
    return events
