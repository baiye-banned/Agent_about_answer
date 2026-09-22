"""回归（issue #141）：会话删除与「仍在飞的流」交错后，轨迹事件的 index 必须仍然自洽。

`chat_trace_sessions.events` 里每个事件都带一个 `index`，它是这段审计记录**唯一的**顺序依据。
修复前这条链会写出重复序号：

    TraceRecorder() 落库 -> attach(cid) -> add x2 -> 并发删除会话 -> finish(cid)
    -> 后台补写 append_trace_event -> indices = [1, 2, 1]

根因不是「删除没清干净」，而是**写路径借用了读取面的守卫**：`append_trace_event` 用
`crud_trace.get_trace_snapshot` 取「已有事件数」，而那条读路径在会话不存在时对视同不可读的
行返回 `None`（issue #127 的孤儿行守卫），调用方把 `None` 当成「这行没有事件」，
于是从 1 重新计号，写进一个已经有 N 条事件的行。

因此本文件的用例分两类：

- **序号面**：无论删除请求落在哪个时点，落库的 index 都必须等于该行 events 里的位置（1..N）；
- **读取面**：修复不得靠「放宽读取守卫」来换序号自洽——#127 锁住的 404 行为必须原样保留。

时序用同一个辅助函数按 `delete_at` 参数切换，逐臂覆盖删除请求与流收尾的先后：
`mid_stream`（流没收尾就删，issue 的复现形态）、`after_finish`（收尾后才删）。
"""

import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from crud import chat as crud_chat
from crud import trace as crud_trace
from database.session import Base
from model.models import ChatTraceSession, Conversation, KnowledgeBase, Message, RevokedToken, User
from rag.learning_trace import TraceRecorder, append_trace_event


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_text(_type, _compiler, **_kwargs):
    """MySQL 的 LONGTEXT 在 sqlite 上按 TEXT 建表（与其它轨迹用例一致）。"""
    return "TEXT"


CONVERSATION_ID = "conv-141"
OTHER_CONVERSATION_ID = "conv-other-141"


@pytest.fixture
def api(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
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
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    db = Session()

    alice = User(username="alice", password_hash="x")
    db.add(alice)
    db.commit()
    db.add(Conversation(id=CONVERSATION_ID, user_id=alice.id, title="seq"))
    db.add(Conversation(id=OTHER_CONVERSATION_ID, user_id=alice.id, title="other"))
    db.commit()

    # crud.trace 自带 SessionLocal（不经过 FastAPI 的 get_db 依赖），必须换成同一个测试库，
    # 否则会绕过测试库去连真实 MySQL。每次调用开新 session，与生产一致。
    monkeypatch.setattr(crud_trace, "SessionLocal", lambda: Session())

    yield _Api(db=db, alice=alice)
    db.close()


class _Api:
    def __init__(self, db, alice):
        self.db = db
        self.alice = alice


def _events(db, trace_id):
    db.expire_all()
    row = db.query(ChatTraceSession).filter_by(id=trace_id).first()
    return crud_trace._load_events(row.events) if row else []


def _indices(db, trace_id):
    return [event.get("index") for event in _events(db, trace_id)]


def _assert_monotonic(db, trace_id, expected_count):
    """index 必须等于它在该行 events 里的位置——这是这段记录唯一的顺序依据。"""
    indices = _indices(db, trace_id)
    assert indices == list(range(1, expected_count + 1)), (
        f"轨迹事件序号不是 1..{expected_count}：{indices}（重复序号让这段记录无法自证顺序）"
    )


async def _run_stream(api, *, delete_at=None, trace_id=None):
    """按生产时序跑一轮轨迹，`delete_at` 决定并发删除请求落在哪个时点。

    每一步都对应 chat_service.py 的真实调用点，不使用替身。`add`/`attach`/`finish` 与生产
    一样是协程，写库交给工作线程（issue #201），所以这里同样 await。
    """
    trace = TraceRecorder(user_id=api.alice.id)
    if trace_id:
        # 只固定 id 以拿到句柄；落库路径本身仍是生产路径。
        trace.trace_id = trace_id
        trace._persist()
    await trace.attach(conversation_id=CONVERSATION_ID)        # chat_service.py:371
    await trace.add("request_received", "stream_chat")         # 事件 1
    await trace.add("input_normalized", "stream_chat")         # 事件 2

    if delete_at == "mid_stream":
        # 并发的 DELETE /api/chat/conversations/{cid} 请求：同一个事务里删轨迹行与会话行。
        crud_chat.delete_conversation(api.db, CONVERSATION_ID, api.alice.id)

    # 流收尾：chat_service.py:791（_safe_trace_finish(..., conversation_id=cid)）
    await trace.finish("done", conversation_id=CONVERSATION_ID, message_id=None)

    if delete_at == "after_finish":
        crud_chat.delete_conversation(api.db, CONVERSATION_ID, api.alice.id)

    # 后台补写：ragas_eval / memory_service 拿着同一个 trace_id 追加。
    append_trace_event(trace.trace_id, "ragas_running", "evaluate_message_async")
    return trace


def test_live_conversation_keeps_indices_monotonic(api):
    """对照臂：会话存活时序号本就是 1、2、3——证明仪器可用，不是「恒绿」。"""
    trace = asyncio.run(_run_stream(api))

    _assert_monotonic(api.db, trace.trace_id, 3)
    assert crud_trace.get_trace_snapshot(trace.trace_id) is not None, (
        "会话还在时轨迹本就该可读；读不到说明用例前提不成立"
    )


def test_delete_before_finish_keeps_indices_monotonic(api):
    """issue #141 的复现形态：删除落在流收尾之前，收尾把行写回，后台补写接在后面。

    修复前这里会拿到 [1, 2, 1]：补写经由读取守卫取长度，读不到就当成「没有事件」从 1 重排。
    """
    trace = asyncio.run(_run_stream(api, delete_at="mid_stream"))

    _assert_monotonic(api.db, trace.trace_id, 3)


def test_delete_before_finish_still_hides_the_row(api):
    """读取面不得为序号让路：写回的行仍然读不出来（#127 锁住的行为原样保留）。"""
    trace = asyncio.run(_run_stream(api, delete_at="mid_stream"))

    assert crud_trace.get_trace_snapshot(trace.trace_id) is None, (
        "会话已删，重写的轨迹行不该重新变得可读——序号自洽不能靠放宽读取守卫来换"
    )


def test_delete_after_finish_does_not_resurrect_the_row(api):
    """另一种时序：收尾之后才删。行随会话一起没了，此后的补写只能是无处可写的空操作。"""
    trace = asyncio.run(_run_stream(api, delete_at="after_finish"))

    api.db.expire_all()
    row = api.db.query(ChatTraceSession).filter_by(id=trace.trace_id).first()
    assert row is None, "会话与轨迹行都已删除，后台补写不该把行重新建回来"


def test_repeated_background_appends_keep_indices_monotonic(api):
    """同一行上连续多次后台补写（RAGAS 在工作线程、记忆摘要在事件循环，同一个 trace_id）。"""
    async def run():
        recorder = TraceRecorder(user_id=api.alice.id)
        await recorder.attach(conversation_id=CONVERSATION_ID)
        await recorder.add("request_received", "stream_chat")
        await recorder.finish("done", conversation_id=CONVERSATION_ID)
        return recorder

    trace = asyncio.run(run())

    for stage in ("ragas_running", "ragas_metric_done", "memory_summary_update_started"):
        append_trace_event(trace.trace_id, stage, "evaluate_message_async")

    _assert_monotonic(api.db, trace.trace_id, 4)


def test_append_path_does_not_consult_the_read_snapshot(api, monkeypatch):
    """根因面：补写路径不得再去问读取面「这行能不能读」。

    #141 坏的从来不是删除的清理范围，而是写路径借用了读取面的守卫拿长度。只断言序号的话，
    序号是可能靠落库侧兜对的——这条用例把根因本身钉住：写路径不许调用带会话存活守卫的
    `get_trace_snapshot`。
    """
    async def run():
        recorder = TraceRecorder(user_id=api.alice.id)
        await recorder.attach(conversation_id=CONVERSATION_ID)
        await recorder.add("request_received", "stream_chat")
        return recorder

    trace = asyncio.run(run())

    def _bomb(*_args, **_kwargs):
        raise AssertionError("补写路径调用了读取面的 get_trace_snapshot（#141 的根因）")

    monkeypatch.setattr(crud_trace, "get_trace_snapshot", _bomb)

    append_trace_event(trace.trace_id, "ragas_running", "evaluate_message_async")

    _assert_monotonic(api.db, trace.trace_id, 2)


def test_index_follows_row_content_not_the_read_guard(api):
    """序号只跟「这行到底存了什么」有关，跟「这行能不能被读出内容」无关。

    直接造一行「会话已删、行还在」的孤儿行（删除链路上的清理追不回来的历史残留形态），
    再补写：读取面看不到它，序号却必须接着行上已有的条数往下排。
    """
    trace_id = "trace-orphan-141"
    api.db.add(ChatTraceSession(
        id=trace_id,
        user_id=api.alice.id,
        conversation_id="conv-deleted-before-the-fix",
        status="done",
        events=json.dumps([{"index": 1}, {"index": 2}], ensure_ascii=False),
    ))
    api.db.commit()

    assert crud_trace.get_trace_snapshot(trace_id) is None, "用例前提不成立：孤儿行本就该读不到"

    append_trace_event(trace_id, "ragas_running", "evaluate_message_async")

    _assert_monotonic(api.db, trace_id, 3)
