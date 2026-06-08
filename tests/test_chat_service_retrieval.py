import asyncio
import json

from schema.schemas import ChatRequest
from service import chat_service


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
        self.commits = 0
        self.closed = False

    def query(self, model):
        if model is chat_service.User:
            return _FakeQuery([self.user])
        if model is chat_service.Conversation:
            return _FakeQuery([])
        return _FakeQuery([])

    def add(self, item):
        self.added.append(item)

    def commit(self):
        self.commits += 1

    def refresh(self, item):
        if getattr(item, "id", None) is None:
            item.id = 1

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


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


def test_stream_chat_uses_direct_advanced_rag_retriever(monkeypatch):
    calls = {"retrieve": []}
    fake_db = _FakeDb(_FakeUser())

    monkeypatch.setattr(chat_service, "decode_token", lambda authorization: "alice")
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: fake_db)
    monkeypatch.setattr(chat_service, "TraceRecorder", _FakeTrace)
    monkeypatch.setattr(chat_service, "resolve_knowledge_base", lambda db, kid: _FakeKnowledgeBase())
    async def fake_build_effective_question(question, attachments):
        return question, {"status": "skipped"}

    monkeypatch.setattr(chat_service, "_build_effective_question", fake_build_effective_question)
    async def fake_recent_memory_text(*_args, **_kwargs):
        return ""

    def fake_memory_context(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(chat_service, "_build_recent_memory_text", fake_recent_memory_text)
    monkeypatch.setattr(chat_service, "_build_memory_context", fake_memory_context)
    monkeypatch.setattr(chat_service, "_build_memory_aware_retrieval_question", lambda question, memory_context: question)

    async def fake_decide_need_rag(*_args, **_kwargs):
        return {"need_rag": True, "route": "rag", "confidence": 1.0, "source": "test", "reason": "needs retrieval"}

    async def fake_retrieve_knowledge(question, knowledge_base_id, db, trace_recorder=None):
        calls["retrieve"].append((question, knowledge_base_id, db, trace_recorder))
        return (
            [{"file_name": "制度.txt", "content": "迟到规则", "file_id": 1, "chunk_id": "a"}],
            {"query_plan": {"keywords": ["迟到"]}, "routes": [], "rrf": [], "rerank": {"status": "done", "items": []}},
        )

    async def fake_stream_rag_answer(*_args, **_kwargs):
        yield "回答"

    monkeypatch.setattr(chat_service, "decide_need_rag", fake_decide_need_rag)
    monkeypatch.setattr(chat_service, "retrieve_knowledge", fake_retrieve_knowledge)
    monkeypatch.setattr(chat_service, "stream_rag_answer", fake_stream_rag_answer)
    monkeypatch.setattr(chat_service, "_trace_sse_payloads", lambda trace: [])
    monkeypatch.setattr(chat_service, "_build_sources", lambda chunks: [{"file": chunks[0]["file_name"]}])
    monkeypatch.setattr(chat_service, "_attach_grounding_trace", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "_safe_trace_attach", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "_safe_trace_finish", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "_schedule_memory_summary_update", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "schedule_ragas_evaluation", lambda *args, **kwargs: None)

    response = asyncio.run(chat_service.stream_chat(ChatRequest(question="迟到怎么处理"), authorization="Bearer token"))
    body = asyncio.run(_collect_stream(response.body_iterator))

    assert len(calls["retrieve"]) == 1
    assert calls["retrieve"][0][0] == "迟到怎么处理"
    assert calls["retrieve"][0][1] == 3
    assert "回答" in body
    assert "\"type\": \"sources\"" in body


async def _collect_stream(iterator):
    chunks = []
    async for chunk in iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return "".join(chunks)
