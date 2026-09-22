"""回归（issue #127）：删除会话必须一并清理该会话的学习轨迹。

修复前删除会话只清会话行、随会话级联的消息与 checkpointer 线程，
`chat_trace_sessions` 里的行会永久留存（`events` 是 LONGTEXT，落库的是完整提问原文与
检索上下文），而且会话删掉之后 `GET /api/chat/traces/{trace_id}` 仍能读到整段轨迹。

用例分两层锁住行为：

- ORM 级：删除后轨迹行真的没了；**只**删该会话的行——同用户其它会话的轨迹必须留着，
  否则「把表清空」也能让上面的断言通过；
- 接口级：删除后两条轨迹读取路径（`/api/chat/traces/{trace_id}` 与
  `/api/chat/messages/{message_id}/trace`）都取不到内容。

另有两条不随主路径走的用例：会话删除前就被孤立的历史轨迹行不能再被读到；删除会话与
「仍在飞的流继续落库」交错时，被重新写回的行同样不能读出内容。
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from crud import trace as crud_trace
from database import checkpointer
from database import session as db_session
from database.session import Base
from model.models import ChatTraceSession, Conversation, KnowledgeBase, Message, RevokedToken, User
from rag.learning_trace import TraceRecorder
from router import chat as chat_router
from router import checkpointer as checkpointer_router
from service import auth_service


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_text(_type, _compiler, **_kwargs):
    """MySQL 的 LONGTEXT 在 sqlite 上按 TEXT 建表（与其它接口级用例一致）。"""
    return "TEXT"


CONVERSATION_ID = "conv-127"
OTHER_CONVERSATION_ID = "conv-other-127"
TRACE_ID = "trace-127"
OTHER_TRACE_ID = "trace-other-127"
ORPHAN_TRACE_ID = "trace-orphan-127"
# 提问原文：轨迹里落库的就是这种不该在会话删除后还留下的内容。
QUESTION = "2026 年差旅报销标准与住宿上限分别是多少？"
OTHER_QUESTION = "另一个会话的问题，删别的会话时不该被波及。"


def _trace_events(trace_id, conversation_id, question):
    """按 TraceRecorder 实际落库的形状造事件（提问原文出现在 params/creates 里）。"""
    return [
        {
            "index": 1,
            "time": "2026-09-21T10:00:00",
            "stage": "request_received",
            "function": "stream_chat",
            "creates": {"trace_id": trace_id},
            "uses": {},
            "params": {"conversation_id": conversation_id, "question": question},
            "result": {},
            "note": "",
        },
        {
            "index": 2,
            "time": "2026-09-21T10:00:01",
            "stage": "input_normalized",
            "function": "stream_chat",
            "creates": {"raw_question": question, "display_question": question},
            "uses": {},
            "params": {},
            "result": {},
            "note": "",
        },
    ]


def _add_trace(db, trace_id, conversation_id, question, message_id=None, user_id=None):
    db.add(ChatTraceSession(
        id=trace_id,
        user_id=user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        status="done",
        events=json.dumps(_trace_events(trace_id, conversation_id, question), ensure_ascii=False),
    ))
    db.commit()
    return trace_id


@pytest.fixture()
def api(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__, RevokedToken.__table__,
            KnowledgeBase.__table__,
            Conversation.__table__,
            Message.__table__,
            ChatTraceSession.__table__,
        ],
    )
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()

    alice = User(username="alice", password_hash="x")
    bob = User(username="bob", password_hash="x")
    db.add_all([alice, bob])
    db.commit()

    db.add_all([
        Conversation(id=CONVERSATION_ID, user_id=alice.id, title="差旅制度"),
        Conversation(id=OTHER_CONVERSATION_ID, user_id=alice.id, title="另一个会话"),
    ])
    db.commit()

    user_message = Message(conversation_id=CONVERSATION_ID, role="user", content=QUESTION)
    assistant_message = Message(
        conversation_id=CONVERSATION_ID,
        role="assistant",
        content="住宿上限 500 元。",
        retrieval_trace=json.dumps(
            {"learning_trace": {"trace_id": TRACE_ID, "status": "done", "event_count": 2}},
            ensure_ascii=False,
        ),
    )
    other_message = Message(conversation_id=OTHER_CONVERSATION_ID, role="user", content=OTHER_QUESTION)
    db.add_all([user_message, assistant_message, other_message])
    db.commit()

    _add_trace(db, TRACE_ID, CONVERSATION_ID, QUESTION,
               message_id=assistant_message.id, user_id=alice.id)
    _add_trace(db, OTHER_TRACE_ID, OTHER_CONVERSATION_ID, OTHER_QUESTION, user_id=alice.id)

    # 真实的删除链路会写 checkpointer 的 sqlite 文件，指到 tmp 以免在仓库目录留产物。
    monkeypatch.setattr(checkpointer, "CHECKPOINTER_DB_PATH", str(tmp_path / "checkpointer.db"))
    # 轨迹读写（crud.trace）自带 SessionLocal，不经过 FastAPI 的 get_db 依赖，必须单独替换成
    # 同一个 sqlite 会话；否则这些用例会绕过测试库去连真实的 MySQL（要么报错，要么读错库）。
    monkeypatch.setattr(crud_trace, "SessionLocal", lambda: db)

    app = FastAPI()
    app.include_router(chat_router.router)
    app.include_router(checkpointer_router.router)
    app.dependency_overrides[db_session.get_db] = lambda: db
    app.dependency_overrides[auth_service.get_current_user] = lambda: alice

    try:
        yield SimpleNamespace(
            client=TestClient(app),
            db=db,
            alice=alice,
            bob=bob,
            user_message_id=user_message.id,
            assistant_message_id=assistant_message.id,
            other_message_id=other_message.id,
        )
    finally:
        db.close()


def _trace_rows(db):
    db.expire_all()
    return {row.id: row for row in db.query(ChatTraceSession).all()}


def _delete_conversation(api, cid=CONVERSATION_ID):
    response = api.client.delete(f"/api/chat/conversations/{cid}")
    assert response.status_code == 200, response.text
    return response


def test_delete_conversation_purges_trace_rows_and_keeps_other_conversations(api):
    """ORM 级：删除会话后该会话的轨迹行消失，同用户其它会话的轨迹行必须留着。"""
    _delete_conversation(api)

    rows = _trace_rows(api.db)
    assert TRACE_ID not in rows, "会话已删，chat_trace_sessions 里仍留着这个会话的轨迹行"
    assert OTHER_TRACE_ID in rows, "删除一个会话把另一个会话的轨迹也删了，清理范围越界"

    surviving_events = "".join(row.events or "" for row in rows.values())
    assert QUESTION not in surviving_events, "提问原文仍留在 chat_trace_sessions 里"


def test_trace_written_by_real_recorder_is_purged_with_conversation(api):
    """按 issue 复现法走**生产写入路径**：TraceRecorder 落库的轨迹同样随会话删除被清掉。

    上一条用例直接 INSERT 出行，这里改用真实的 TraceRecorder（只固定 trace_id 以拿到句柄，
    不跑模型调用），避免「夹具造的行形状与生产写入不一致」造成的假绿。
    """
    trace_id = "trace-recorder-127"
    recorder = TraceRecorder(user_id=api.alice.id)
    recorder.trace_id = trace_id

    async def _write_trace():
        # 与生产同一口径：add/attach/finish 都是协程，写库交给工作线程（issue #201）。
        await recorder.attach(conversation_id=CONVERSATION_ID)
        await recorder.add("input_normalized", "stream_chat", creates={"effective_question": QUESTION})
        await recorder.finish("done", conversation_id=CONVERSATION_ID, message_id=api.assistant_message_id)

    asyncio.run(_write_trace())

    assert trace_id in _trace_rows(api.db), "生产写入路径没落库，用例前提不成立"
    assert api.client.get(f"/api/chat/traces/{trace_id}").status_code == 200, (
        "会话还在时轨迹本就该可读；读不到说明用例前提不成立"
    )

    _delete_conversation(api)

    assert trace_id not in _trace_rows(api.db), "生产写入路径产生的轨迹没有随会话删除被清掉"
    assert api.client.get(f"/api/chat/traces/{trace_id}").status_code == 404


def test_delete_conversation_removes_conversation_and_messages(api):
    """删除会话本身的既有行为不能因本次改动而改变。"""
    _delete_conversation(api)

    api.db.expire_all()
    assert api.db.query(Conversation).filter_by(id=CONVERSATION_ID).first() is None
    assert api.db.query(Message).filter_by(conversation_id=CONVERSATION_ID).count() == 0
    assert api.db.query(Conversation).filter_by(id=OTHER_CONVERSATION_ID).first() is not None


def test_trace_endpoint_does_not_serve_trace_of_deleted_conversation(api):
    """接口级（读取路径一）：会话删除后 /api/chat/traces/{trace_id} 必须取不到内容。"""
    _delete_conversation(api)

    response = api.client.get(f"/api/chat/traces/{TRACE_ID}")
    assert response.status_code == 404, (
        f"会话已删除，轨迹接口仍返回内容：{response.status_code} {response.text}"
    )
    assert QUESTION not in response.text


def test_message_trace_endpoint_does_not_serve_trace_of_deleted_conversation(api):
    """接口级（读取路径二）：messages/{id}/trace 同样取不到已删会话的轨迹。

    这条路径修复前后都应为 404——它先按 message_id 查消息，而消息随会话级联删除。
    列在这里是为了固定「轨迹读取面」的穷举结论：两条读取路径都不再可达。
    """
    _delete_conversation(api)

    for message_id in (api.user_message_id, api.assistant_message_id):
        response = api.client.get(f"/api/chat/messages/{message_id}/trace")
        assert response.status_code == 404, (
            f"消息已随会话级联删除，该路径仍返回内容：{response.status_code} {response.text}"
        )
        assert QUESTION not in response.text


def test_no_read_endpoint_serves_deleted_conversation_content(api):
    """对抗面穷举：删除会话后把所有相关读取入口扫一遍，任何一处都不能再吐回原文。

    覆盖前端实际调用的两条轨迹入口，加上会话列表、消息历史与 checkpointer 线程列表——
    「删会话」这条链路要一起清掉的东西都要扫到。最后一条断言是**阳性对照**：另一个会话的
    内容必须照常读得到，否则「全都 404」也能让本用例通过。
    """
    _delete_conversation(api)

    must_404 = [
        f"/api/chat/traces/{TRACE_ID}",
        f"/api/chat/messages/{api.assistant_message_id}/trace",
        f"/api/chat/conversations/{CONVERSATION_ID}",
    ]
    for path in must_404:
        response = api.client.get(path)
        assert response.status_code == 404, f"{path} 期望 404，实际 {response.status_code}"
        assert QUESTION not in response.text, f"{path} 仍能读出已删会话的提问原文"

    # 列表类入口返回 200，这里断言的是「内容不在响应里」。
    for path in ("/api/chat/conversations", "/api/checkpointer/threads"):
        response = api.client.get(path)
        assert response.status_code == 200, f"{path} 期望 200，实际 {response.status_code}"
        assert QUESTION not in response.text, f"{path} 仍能读出已删会话的提问原文"
        assert CONVERSATION_ID not in response.text, f"{path} 仍暴露已删会话的 id"

    # 阳性对照：没被删的会话照常可读，证明上面的断言不是「整个读取面都读不到」。
    survivor = api.client.get(f"/api/chat/conversations/{OTHER_CONVERSATION_ID}")
    assert survivor.status_code == 200, survivor.text
    assert OTHER_QUESTION in survivor.text, "阳性对照失败：幸存会话的内容也读不到了"


def test_trace_endpoint_hides_trace_orphaned_before_this_fix(api):
    """守护：修复前删掉的会话留下的孤儿轨迹行，也不能再被读到。

    这类行的会话行早已不存在，删除链路上的清理追不回来，只能由读取侧兜住。
    """
    _add_trace(api.db, ORPHAN_TRACE_ID, "conv-deleted-before-the-fix", QUESTION,
               user_id=api.alice.id)

    response = api.client.get(f"/api/chat/traces/{ORPHAN_TRACE_ID}")
    assert response.status_code == 404, (
        f"所属会话不存在的轨迹仍能读出内容：{response.status_code} {response.text}"
    )
    assert QUESTION not in response.text


def test_trace_written_after_delete_cannot_be_read_back(api):
    """并发面：删除会话与「仍在飞的流继续落库」交错时，重写的行不能读出内容。

    会话删除发生在一轮问答的流还没结束时，TraceRecorder 后续的落库会把这行重新写回去
    （persist_trace_session 在行不存在时新建）。这里用真实落库路径复现该交错，锁住
    「即使行被写回，内容也读不出来」。
    """
    _delete_conversation(api)

    recorder = TraceRecorder(user_id=api.alice.id)
    recorder.trace_id = TRACE_ID

    async def _write_trace():
        await recorder.attach(conversation_id=CONVERSATION_ID)
        await recorder.add("generation_started", "stream_rag_answer", params={"question": QUESTION})
        await recorder.finish("done", conversation_id=CONVERSATION_ID)

    asyncio.run(_write_trace())

    rows = _trace_rows(api.db)
    # 如实记录：这行会被写回（存储面残留），断言的是读取面。
    assert TRACE_ID in rows, "本用例假定重写确实发生了；没发生则说明落库路径变了，需重新评估"

    response = api.client.get(f"/api/chat/traces/{TRACE_ID}")
    assert response.status_code == 404, (
        f"删除会话后重写的轨迹仍能读出内容：{response.status_code} {response.text}"
    )
    assert QUESTION not in response.text


def test_delete_conversation_succeeds_when_learning_trace_disabled(api, monkeypatch):
    """LEARNING_TRACE_ENABLED=false 的路径不回归：关掉轨迹后删除会话照常成功。"""
    import rag.learning_trace as learning_trace

    monkeypatch.setattr(learning_trace, "LEARNING_TRACE_ENABLED", False)

    _delete_conversation(api)

    rows = _trace_rows(api.db)
    assert TRACE_ID not in rows
    assert OTHER_TRACE_ID in rows, "关掉轨迹开关后删除会话波及了其它会话的轨迹"
