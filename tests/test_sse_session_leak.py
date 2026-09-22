"""回归（issue #58）：客户端断开 SSE 流时必须关闭数据库会话，避免连接池被逐步耗尽。

两个复现现象各固化成一个用例：

1. 断开 → 会话关闭、trace 落到终态。
   读若干帧后 `aclose()` body_iterator —— 生成器在挂起的 yield 处收到 `GeneratorExit`。
   修复前 `db.close()` 写在生成器函数体末尾的最后一个 yield 之前，断开后不会执行，
   `chat_trace_sessions.status` 也永远停在 `running`。
   `test_starlette_disconnect_cancellation_closes_session` 另按**真实**的 Starlette 语义复现：
   `StreamingResponse` 收到 `http.disconnect` 后取消正在跑流的任务，把 `CancelledError`
   抛进挂起的 yield（Starlette 0.38 不调用 `aclose()`）。两者都继承自 `BaseException`。
2. 断开 → 连接归还连接池，后续请求不再超时。
   真实的 `QueuePool(pool_size=1, max_overflow=0)`：借出的连接不归还时池立刻耗尽，
   后续请求抛 `QueuePool limit of size 1 overflow 0 reached ... connection timed out`。

注意：涉及连接池计数的场景必须在同一个事件循环内完成。`asyncio.run` 退出时会执行
`shutdown_asyncgens()`，把仍存活的异步生成器一并 `aclose()`；跨 `asyncio.run` 观测会得到
「已经被关闭」的假象，测不出断开路径的真实行为。
"""

import asyncio

import anyio
import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

import rag.llm as llm
from conftest import FakeKnowledgeBase, collect_stream, parse_sse_frames, streamed_content
from database.session import Base
from model.models import Conversation, Message, RevokedToken, User
from schema.schemas import ChatRequest
from service import chat_service


CHUNK = {"file_name": "制度.txt", "content": "迟到规则", "file_id": 1, "chunk_id": "a"}


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_text(_type, _compiler, **_kwargs):
    """MySQL 的 LONGTEXT 在 sqlite 上按 TEXT 建表（与 test_knowledge_ownership 一致）。"""
    return "TEXT"


def _patch_boundaries(monkeypatch, fake_db, trace_cls):
    """只打桩真正的边界（鉴权/会话工厂/trace/知识库解析/检索/模型），其余跑真实实现。"""
    monkeypatch.setattr(
        chat_service,
        "authenticate",
        lambda db, authorization: db.query(User).filter_by(username="alice").first(),
    )
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: fake_db)
    monkeypatch.setattr(chat_service, "TraceRecorder", trace_cls)
    monkeypatch.setattr(chat_service, "resolve_knowledge_base", lambda db, kid, user_id: FakeKnowledgeBase())

    async def fake_build_effective_question(question, attachments):
        return question, {"status": "skipped"}

    async def fake_recent_memory_text(*_args, **_kwargs):
        return ""

    async def fake_decide_need_rag(*_args, **_kwargs):
        return {"need_rag": True, "route": "rag", "confidence": 1.0, "source": "test", "reason": "needs retrieval"}

    async def fake_retrieve_knowledge(question, knowledge_base_id, db, trace_recorder=None):
        return (
            [dict(CHUNK)],
            {"query_plan": {}, "routes": [], "rrf": [], "rerank": {"status": "done", "items": []}},
        )

    monkeypatch.setattr(chat_service, "_build_effective_question", fake_build_effective_question)
    monkeypatch.setattr(chat_service, "_build_recent_memory_text", fake_recent_memory_text)
    monkeypatch.setattr(chat_service, "decide_need_rag", fake_decide_need_rag)
    monkeypatch.setattr(chat_service, "retrieve_knowledge", fake_retrieve_knowledge)
    monkeypatch.setattr(chat_service, "_schedule_memory_summary_update", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "schedule_ragas_evaluation", lambda *args, **kwargs: None)

    async def real_chain(question, context, memory_context, trace, use_rag=True):
        async for event in llm.stream_answer_events(question, context, memory_context, trace, use_rag):
            yield event

    monkeypatch.setattr(chat_service, "stream_rag_answer", real_chain)
    monkeypatch.setattr(llm, "DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "TEXT_FALLBACK_API_KEY", "sk-test")

    async def fake_stream(model, messages):
        for piece in ["根据《员工手册》", "迟到30分钟以内罚款50元。"]:
            yield piece

    monkeypatch.setattr(llm, "_stream_model_chunks", fake_stream)


def _disconnect_after(response, n_frames):
    """读 n_frames 帧后 aclose()，模拟客户端中途断开。"""

    async def _run():
        iterator = response.body_iterator
        for _ in range(n_frames):
            await iterator.__anext__()
        await iterator.aclose()

    asyncio.run(_run())


@pytest.mark.parametrize("n_frames", [1, 3, 6])
def test_disconnect_closes_db_session_and_ends_trace(monkeypatch, fake_db, trace_recorder_cls, n_frames):
    """断开路径必须关闭会话，并给 trace 落一个非 running 的终态。"""
    _patch_boundaries(monkeypatch, fake_db, trace_recorder_cls)

    response = asyncio.run(
        chat_service.stream_chat(ChatRequest(question="迟到30分钟以内怎么罚款？"), authorization="Bearer token")
    )
    _disconnect_after(response, n_frames)

    trace = trace_recorder_cls.instances[-1]
    assert fake_db.closed is True, "客户端断开后 db.close() 没有执行，连接会一直挂在池外"
    assert trace.status != "running", "断开后 chat_trace_sessions.status 停在 running，没有落终态"
    assert trace.status == "failed"


def test_starlette_disconnect_cancellation_closes_session(monkeypatch, fake_db, trace_recorder_cls):
    """按真实 Starlette 语义复现：http.disconnect → 取消任务 → CancelledError 抛进挂起的 yield。

    上一条用例用 `aclose()`（GeneratorExit）；Starlette 0.38 的 `StreamingResponse` 走的是
    `listen_for_disconnect` + task group 取消，抛进去的是 `CancelledError`。两者都是 BaseException，
    都必须走同一个 finally 收尾——本用例驱动真实的 `StreamingResponse.__call__` 来锁住这一点。
    """
    _patch_boundaries(monkeypatch, fake_db, trace_recorder_cls)

    async def slow_stream(model, messages):
        """真实模型分片之间有网络等待；留出挂起点，取消才落在流的中途而不是跑完之后。"""
        for piece in ["根据《员工手册》", "迟到30分钟以内罚款50元。"]:
            yield piece
            await anyio.sleep(0.05)

    monkeypatch.setattr(llm, "_stream_model_chunks", slow_stream)

    async def _run():
        response = await chat_service.stream_chat(
            ChatRequest(question="迟到30分钟以内怎么罚款？"), authorization="Bearer token"
        )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/chat/stream",
            "raw_path": b"/chat/stream",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "server": ("testserver", 80),
            "client": ("testclient", 123),
        }
        sent = []
        first_receive = True

        async def receive():
            nonlocal first_receive
            if first_receive:
                first_receive = False
                return {"type": "http.request", "body": b"", "more_body": False}
            # 让流先真的产出几帧再报告断开（真实客户端也是先收几帧再断）；
            # 轮询有上界，避免流异常短时把用例挂死。
            for _ in range(200):
                if len(sent) >= 3:
                    break
                await anyio.sleep(0.01)
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        await response(scope, receive, send)
        return sent

    sent = asyncio.run(_run())

    # 前提断言：断开必须落在流的中途（没有收到 more_body=False 的终止帧），
    # 否则本用例跑的其实是正常完成路径，覆盖不到取消语义。
    assert len(sent) >= 3, f"流还没产出就结束了，本用例没覆盖到取消路径：{sent}"
    assert sent[-1]["type"] == "http.response.body" and sent[-1]["more_body"] is True, (
        f"断开没有落在流的中途：{sent[-1]}"
    )

    trace = trace_recorder_cls.instances[-1]
    assert fake_db.closed is True, "Starlette 取消路径下 db.close() 没有执行"
    assert trace.status == "failed"


def test_disconnect_returns_connection_to_pool(monkeypatch, tmp_path, trace_recorder_cls):
    """断开一次后连接必须归还池：pool_size=1 时后续请求仍然可用。

    issue #187 之前，流式请求整段握着一个请求级 Session，所以「流进行到一半时池里恰好有
    1 条连接被占着」是本用例的**前提**。现在每次写库各自「开 session → 用 → 关」，这条
    连接不会再跨着整段流被占住，前提随之变成 0——它同时仍是泄漏断言：任何一段同步 DB 工作
    忘了收会话（或又把 Session 跨 await 握在手里），这里立刻非 0，#58 的漏连接形态就回来了。
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'leak.db'}",
        connect_args={"check_same_thread": False},
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[User.__table__, RevokedToken.__table__, Conversation.__table__, Message.__table__],
    )
    with sessionmaker(bind=engine)() as seed:
        seed.add(User(id=7, username="alice", password_hash="x"))
        seed.commit()

    testing_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    _patch_boundaries(monkeypatch, None, trace_recorder_cls)
    monkeypatch.setattr(chat_service, "SessionLocal", testing_session)

    async def _scenario():
        first = await chat_service.stream_chat(
            ChatRequest(question="迟到怎么罚款？"), authorization="Bearer token"
        )
        iterator = first.body_iterator
        await iterator.__anext__()
        # 流式期间不得有连接被占着（issue #187 之后每次写库自带会话、当场归还）。
        checked_out_while_streaming = engine.pool.checkedout()
        await iterator.aclose()
        released_after_disconnect = engine.pool.checkedout()

        second = await chat_service.stream_chat(
            ChatRequest(question="第二次提问"), authorization="Bearer token"
        )
        body = [chunk async for chunk in second.body_iterator]
        return checked_out_while_streaming, released_after_disconnect, "".join(body)

    checked_out_while_streaming, released_after_disconnect, second_body = asyncio.run(_scenario())

    assert checked_out_while_streaming == 0, (
        "流式期间池里仍有连接被占着：说明有会话跨着整段流被持有，断开路径又会漏连接（issue #58）"
    )
    assert released_after_disconnect == 0, "断开后连接没有归还池，pool_size 次断连后连接池会被耗尽"
    # 第二次请求跑到了终止帧，说明池没有被上一次断连拖垮（修复前这里会 QueuePool timeout）
    assert "data: [DONE]" in second_body
    trace = trace_recorder_cls.instances[-1]
    assert trace.status == "done"
    engine.dispose()


def test_normal_completion_keeps_done_status(monkeypatch, fake_db, trace_recorder_cls):
    """整条流正常跑完时，兜底逻辑不得覆盖已有的终态。"""
    _patch_boundaries(monkeypatch, fake_db, trace_recorder_cls)

    response = asyncio.run(
        chat_service.stream_chat(ChatRequest(question="迟到30分钟以内怎么罚款？"), authorization="Bearer token")
    )
    body = collect_stream(response.body_iterator)

    trace = trace_recorder_cls.instances[-1]
    assert trace.status == "done"
    assert fake_db.closed is True
    assert "data: [DONE]" in body
    assert streamed_content(parse_sse_frames(body)) == "根据《员工手册》迟到30分钟以内罚款50元。"
    assert len(fake_db.added_by_role("assistant")) == 1
