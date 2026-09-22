"""回归（issue #201）：学习轨迹的写回必须离开事件循环线程。

#187 把「流式对话与记忆压缩」里**字面写在协程体内**的同步 `Session` 搬到了工作线程，但学习
轨迹这条链不在它的验收边界内：`LEARNING_TRACE_ENABLED` 的生产默认值是 true，
`TraceRecorder` 的 `__init__` / `add` / `attach` / `finish` 会经 `rag/learning_trace.py` 落到
`crud/trace.py` 自己开的同步 `SessionLocal()` 上，而调用方（`stream_chat` / `event_stream`）
就是事件循环线程——一次轨迹写回独占事件循环，同进程所有并发请求的 token 流一起被冻住。

issue 实测（本线在 `643d4a77` 的仓库外副本上复现，`_fix201/red_arm_raw.log`）：窗口 1521ms、
10 条同步 DB 语句、同步调用线程 id 与事件循环线程相同、期间 10ms 心跳 **0** 次。

本文件与 #187 用同一套 oracle，只把被测对象换成 trace 链：

1. **心跳**：同一个事件循环里先跑起心跳协程，再 await 目标协程，调用期间必须被调度过。
2. **线程**：Session 自己记录「开 / 用 / 关」各发生在哪个线程上——三条记录缺一不可，
   只记「用」分不出会话是不是别处开好递过来的，只记「开」证明不了它真的在那个线程上被用过。
   拦截点必须挂在 **`crud.trace`** 上：`chat_service.SessionLocal` 换成测试工厂**看不见**
   这条路径，这正是 issue 里那份形态门禁扫不到它的原因。
3. **语义**：顺序、条数与终态都要与内存里的轨迹一致——写回被搬走不等于可以变成
   「发了不管」：每一次 `await` 返回时，那一行必须已经落库。断连（`aclose()`）路径同理，
   收尾那一次写回也得真的落下去。
4. **形态**：AST 门禁扫「事件循环线程上的轨迹写回」——async def 体内未被 await / 未被
   `asyncio.to_thread` 承载的 `add`/`attach`/`finish`/`_safe_trace_*`/`append_trace_event`。
   扫描器自带 2 正 5 负自检（共 7 条），先证明有区分力再采信 0 命中。
5. **取消语义**：两处包装层都只吞 `Exception`——`CancelledError` / `GeneratorExit` 必须
   原样上抛，且在途的那一次写回不因取消而丢（§5，含正对照证明不是「什么异常都放行」）。

注入的 sleep 是**放大器不是测量值**：本机内存 SQLite 一次查询是 µs 级，不加放大器时
「心跳没被独占」与「压根没跑」不可区分。与耗时、与方言无关的结论来自线程记录。
"""

import ast
import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import rag.learning_trace as learning_trace
import rag.llm as llm
import rag.memory_service as memory_service
import rag.retrieval as retrieval
from config import MEMORY_WINDOW_TURNS
from crud import trace as crud_trace
from database.session import Base
from model.models import ChatTraceSession, Conversation, KnowledgeBase, Message, User
from rag.learning_trace import TraceRecorder
from schema.schemas import ChatRequest
from service import chat_service, trace_service


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_text(_type, _compiler, **_kwargs):
    """MySQL 的 LONGTEXT 在 sqlite 上按 TEXT 建表（与其它轨迹用例一致）。"""
    return "TEXT"


class _RecordingSession(SASession):
    """记下「会话在哪个线程上被开、被用、被关」的 Session（与 #187 同一判据）。

    `delay_seconds` 在每次 execute 前 sleep：内存 SQLite 一次查询是 µs 级，没有这个放大器
    时「同步段跑在循环线程上」根本观测不到。它是放大器，不是被测耗时。
    """

    instances: list["_RecordingSession"] = []
    delay_seconds = 0.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.opened_thread = threading.get_ident()
        self.use_threads: set[int] = set()
        self.closed_thread: int | None = None
        self.statements = 0
        _RecordingSession.instances.append(self)

    def execute(self, *args, **kwargs):
        self.use_threads.add(threading.get_ident())
        self.statements += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return super().execute(*args, **kwargs)

    def commit(self):
        self.use_threads.add(threading.get_ident())
        return super().commit()

    def close(self):
        if self.closed_thread is None:
            self.closed_thread = threading.get_ident()
        self.use_threads.add(threading.get_ident())
        return super().close()

    @classmethod
    def reset(cls, delay_seconds=0.0):
        cls.instances = []
        cls.delay_seconds = delay_seconds


def _session_factory(monkeypatch, *, delay_seconds=0.0):
    """把三个模块各自的 SessionLocal 都换到同一个内存库上。

    `crud.trace` 那一处是本文件的关键：轨迹写回自己开会话，只换 `chat_service.SessionLocal`
    的用例永远看不到它在哪个线程上跑（issue #201 的形态门禁盲区）。
    """
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    _RecordingSession.reset(delay_seconds)
    factory = sessionmaker(bind=engine, class_=_RecordingSession, autocommit=False, autoflush=False)
    monkeypatch.setattr(crud_trace, "SessionLocal", factory)
    monkeypatch.setattr(chat_service, "SessionLocal", factory)
    monkeypatch.setattr(memory_service, "SessionLocal", factory)
    # 生产默认值就是开：本文件测的正是「开」着的时候
    monkeypatch.setattr(learning_trace, "LEARNING_TRACE_ENABLED", True)
    return engine, factory


async def _measure(call, *, tick_seconds=0.01, settle_seconds=0.05):
    """跑起心跳协程再 await 目标协程，返回调用期间的心跳、耗时与循环线程。

    `settle_seconds` 先让心跳真的被调度过：否则「0 次」可能只是心跳还没开始，与被测代码
    无关——先断言基线非零，本用例才不会自欺。
    """
    loop_thread = threading.get_ident()
    beats: list[float] = []
    stop = asyncio.Event()

    async def ticker():
        while not stop.is_set():
            await asyncio.sleep(tick_seconds)
            beats.append(time.perf_counter())

    task = asyncio.create_task(ticker())
    try:
        await asyncio.sleep(settle_seconds)
        assert beats, "心跳协程在计时开始前一次都没被调度，本机环境不满足本用例前提"
        started = time.perf_counter()
        result = await call()
        finished = time.perf_counter()
    finally:
        stop.set()
        await task
    during = [beat for beat in beats if started < beat < finished]
    return {
        "loop_thread": loop_thread,
        "heartbeats": during,
        "elapsed": finished - started,
        "result": result,
    }


def _snapshot_sessions() -> list[_RecordingSession]:
    """在被测调用刚结束时取一份快照：用例自己读库开的会话不算进被测行为。"""
    return list(_RecordingSession.instances)


def _assert_every_session_stayed_on_its_worker_thread(sessions, loop_thread):
    """每个会话都必须「开 → 用 → 关」整段在同一个工作线程里完成，且那线程不是循环线程。"""
    assert sessions, "一个会话都没拦到：SessionLocal 的拦截点没生效，本用例是空跑"
    for session in sessions:
        assert session.use_threads, "拦到一个从未被使用过的会话，说明记录点不对"
        assert session.use_threads == {session.opened_thread}, (
            f"会话跨线程使用：opened={session.opened_thread} used={sorted(session.use_threads)}"
        )
        assert session.closed_thread == session.opened_thread, (
            f"会话没有在开启它的线程上关闭：opened={session.opened_thread} closed={session.closed_thread}"
        )
        assert session.opened_thread != loop_thread, (
            f"会话是开在事件循环线程 {loop_thread} 上的"
        )


def _trace_statements():
    """被拦到的会话里，落到 DB 的语句总数（轨迹链与聊天链混在一起，正对照用）。"""
    return sum(session.statements for session in _RecordingSession.instances)


def _flow(recorder):
    """issue 复现步骤里那串调用，一次轨迹写回的最小完整形状。"""
    async def call():
        await recorder.add("request_received", "stream_chat")
        await recorder.add("input_normalized", "stream_chat")
        await recorder.attach(conversation_id="conv-201")
        await recorder.finish("done")

    return call


# ---------------------------------------------------------------------------
# 1) 核心：写回不在事件循环线程上
# ---------------------------------------------------------------------------


def test_trace_writeback_leaves_the_event_loop_thread(monkeypatch):
    """生产默认（`LEARNING_TRACE_ENABLED=True`）下，一次轨迹写回全程不占事件循环。"""
    engine, _ = _session_factory(monkeypatch, delay_seconds=0.15)
    try:
        measured = asyncio.run(_measure(_flow(TraceRecorder(user_id=7))))
        sessions = _snapshot_sessions()
    finally:
        engine.dispose()

    assert measured["heartbeats"], "轨迹写回期间事件循环一次都没被调度"
    assert measured["elapsed"] >= 0.15, "注入的放大器没有生效，本用例测不到阻塞"
    assert _trace_statements() > 0, "一条同步语句都没有：轨迹压根没走落库路径，本用例是空跑"
    _assert_every_session_stayed_on_its_worker_thread(sessions, measured["loop_thread"])


def test_trace_disabled_writes_nothing(monkeypatch):
    """对照片：关掉 trace 后同一条调用链 0 条语句、心跳照常——证明上面读到的是 trace 链。"""
    engine, _ = _session_factory(monkeypatch, delay_seconds=0.15)
    try:
        monkeypatch.setattr(learning_trace, "LEARNING_TRACE_ENABLED", False)
        flow = _flow(TraceRecorder(user_id=7))

        async def call():
            await flow()
            # 关掉 trace 后这条链一句 DB 都不碰，窗口会缩到几十 µs。补一段会让出的 await，
            # 让「心跳照常」这句可读——否则「0 次心跳」只是窗口太短，与「被独占」不可区分。
            await asyncio.sleep(0.3)

        measured = asyncio.run(_measure(call))
        sessions = _snapshot_sessions()
    finally:
        engine.dispose()

    assert _trace_statements() == 0, "trace 已关仍有同步 DB 语句，对照片不成立"
    assert not sessions, "trace 已关却仍开了会话，对照片不成立"
    assert len(measured["heartbeats"]) >= 5, "对照片段心跳没照常流动，探针读不出东西"


@pytest.mark.parametrize(
    "helper",
    [
        pytest.param(
            lambda recorder: llm._trace_add(recorder, "langchain_generation_prompt_built", "ChatOpenAI"),
            id="llm",
        ),
        pytest.param(
            lambda recorder: retrieval._trace_add(recorder, "retriever_started", "retrieve_knowledge"),
            id="retrieval",
        ),
    ],
)
def test_trace_helpers_offload_to_the_worker_thread(monkeypatch, helper):
    """`rag/llm.py` 与 `rag/retrieval.py` 的 `_trace_add` 各自走同一条卸载路径。

    两个模块分成两条用例：合成一条时，只要其中一个把写回留在循环线程上，另一个仍会产生
    「工作线程上的会话」，合起来的断言照样全绿——那样这个洞就没人盯着了。
    """
    engine, _ = _session_factory(monkeypatch)
    try:
        recorder = TraceRecorder(user_id=7)

        async def call():
            await helper(recorder)

        measured = asyncio.run(_measure(call))
        sessions = _snapshot_sessions()
    finally:
        engine.dispose()

    assert _trace_statements() > 0, "这条 helper 的轨迹写回一条语句都没有，本用例是空跑"
    _assert_every_session_stayed_on_its_worker_thread(sessions, measured["loop_thread"])


# ---------------------------------------------------------------------------
# 2) 语义：顺序、条数与终态不能因为「搬到线程里」而松掉
# ---------------------------------------------------------------------------


def test_trace_row_matches_the_in_memory_events_after_each_await(monkeypatch):
    """每次 `await` 返回时那一行必须已经落库，且事件顺序、序号、终态与内存一致。

    这条是「写回被搬走」与「写回变成发了不管」的分界：任何把落库改成后台任务的写法，
    要么下面这次读拿到旧行，要么终态还停在 running。
    """
    engine, factory = _session_factory(monkeypatch)
    try:
        recorder = TraceRecorder(user_id=7)

        async def call():
            await recorder.add("request_received", "stream_chat")
            await recorder.add("input_normalized", "stream_chat")
            await recorder.attach(conversation_id="conv-201")
            await recorder.finish("done")

        asyncio.run(call())
        sessions = _snapshot_sessions()

        db = factory()
        try:
            row = db.query(ChatTraceSession).filter_by(id=recorder.trace_id).first()
            assert row is not None, "轨迹行压根没落库"
            stored = json.loads(row.events)
            assert [event["stage"] for event in stored] == ["request_received", "input_normalized"]
            assert [event["index"] for event in stored] == [1, 2]
            assert stored == recorder.events, "落库的事件与内存里的不是同一份（丢字段 / 乱序）"
            assert row.status == "done", f"终态没写回去：{row.status}"
            assert row.conversation_id == "conv-201", "attach 的会话绑定没写回去"
        finally:
            db.close()
        _assert_every_session_stayed_on_its_worker_thread(sessions, threading.get_ident())
    finally:
        engine.dispose()


def _seed_conversation(factory, *, turns=0):
    db = factory()
    try:
        db.add(Conversation(id="conv-201", user_id=7, knowledge_base_id=1, title="迟到问题"))
        for index in range(turns):
            db.add(Message(
                conversation_id="conv-201",
                role="user" if index % 2 == 0 else "assistant",
                content=f"第{index}条消息：迟到怎么罚款",
            ))
        db.commit()
    finally:
        db.close()
    # 布场用的会话不算进被测行为
    _RecordingSession.instances = []


def test_memory_summary_update_writes_its_trace_events_off_the_loop(monkeypatch):
    """记忆压缩这条链上的 `append_trace_event`（13 处）同样不得在事件循环线程上落库。

    `_update_memory_summary_from_sliding_window` 是事件循环线程上的协程（issue #187 把它
    自己的 DB 段搬走了），但它顺手补写的轨迹事件走的是**另一条模块**的同步会话——#187 的
    扫描器按词法闭包扫不到，正是 issue #201 点名的盲区。
    """
    engine, factory = _session_factory(monkeypatch, delay_seconds=0.02)
    try:
        _seed_conversation(factory, turns=(MEMORY_WINDOW_TURNS + 2) * 2)

        async def fake_summarize(_previous_summary, _transcript):
            return "合并后的长期摘要"

        monkeypatch.setattr(memory_service, "_summarize_conversation_memory", fake_summarize)

        async def call():
            recorder = TraceRecorder(user_id=7)
            await recorder.add("request_received", "stream_chat")
            await memory_service._update_memory_summary_from_sliding_window("conv-201", recorder.trace_id)
            return recorder

        measured = asyncio.run(_measure(call))
        sessions = _snapshot_sessions()
        trace_id = measured["result"].trace_id

        db = factory()
        try:
            row = db.query(ChatTraceSession).filter_by(id=trace_id).first()
            assert row is not None, "记忆压缩没有写出任何轨迹事件，本用例是空跑"
            stages = [event["stage"] for event in json.loads(row.events)]
            assert "memory_summary_update_triggered" in stages, f"轨迹事件不完整：{stages}"
        finally:
            db.close()
        _assert_every_session_stayed_on_its_worker_thread(sessions, measured["loop_thread"])
    finally:
        engine.dispose()


def test_a_failing_trace_write_does_not_break_the_caller(monkeypatch):
    """轨迹写失败仍然只吞在轨迹这一侧：异常不得冒到主链路（#201 之前的既有契约）。"""
    engine, _ = _session_factory(monkeypatch)
    try:
        def boom(*_args, **_kwargs):
            raise RuntimeError("trace backend down")

        monkeypatch.setattr(crud_trace, "persist_trace_session", boom)
        recorder = TraceRecorder(user_id=7)

        async def call():
            assert await recorder.add("request_received", "stream_chat") != {}
            await recorder.finish("failed")

        asyncio.run(call())
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 3) 端到端：真实 stream_chat + 真实 TraceRecorder（trace 开着）
# ---------------------------------------------------------------------------


def _seed_user_and_knowledge_base(factory):
    db = factory()
    try:
        db.add(User(id=7, username="alice", password_hash="x"))
        db.add(KnowledgeBase(id=1, user_id=7, name="制度库"))
        db.commit()
    finally:
        db.close()
    _RecordingSession.instances = []


def _stub_stream_boundaries(monkeypatch, *, chunks=("迟到30分钟以内罚款50元。",)):
    """只打桩真正的边界（鉴权、模型、检索），TraceRecorder 与两个会话工厂都跑真实实现。"""
    def real_query_authenticate(db, authorization):
        return db.query(User).filter_by(username="alice").first()

    async def fake_decide_need_rag(*_args, **_kwargs):
        return {"need_rag": True, "route": "rag", "confidence": 1.0, "source": "test", "reason": "needs retrieval"}

    async def fake_retrieve_knowledge(question, knowledge_base_id, db, trace_recorder=None):
        return [{"file_name": "制度.txt", "content": "迟到罚款", "file_id": 1, "chunk_id": "a"}], {"routes": []}

    async def fake_stream_rag_answer(*_args, **_kwargs):
        for chunk in chunks:
            yield chunk

    monkeypatch.setattr(chat_service, "authenticate", real_query_authenticate)
    monkeypatch.setattr(chat_service, "decide_need_rag", fake_decide_need_rag)
    monkeypatch.setattr(chat_service, "retrieve_knowledge", fake_retrieve_knowledge)
    monkeypatch.setattr(chat_service, "stream_rag_answer", fake_stream_rag_answer)
    monkeypatch.setattr(chat_service, "schedule_ragas_evaluation", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "_schedule_memory_summary_update", lambda *args, **kwargs: None)


def test_stream_chat_with_trace_enabled_keeps_every_write_on_a_worker_thread(monkeypatch):
    """整条流跑完（trace 开着）：聊天侧与轨迹侧的每一个会话都在工作线程上「开 → 用 → 关」。

    轨迹侧 20 次 add/finish/attach 全在事件循环线程上，只搬 `_persist` 或只搬 `__init__`
    的写法会在这里留下循环线程上的会话。
    """
    engine, factory = _session_factory(monkeypatch, delay_seconds=0.02)
    _seed_user_and_knowledge_base(factory)
    _stub_stream_boundaries(monkeypatch)

    async def call():
        response = await chat_service.stream_chat(
            ChatRequest(question="迟到怎么罚款"), authorization="Bearer token"
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    try:
        measured = asyncio.run(_measure(call))
        sessions = _snapshot_sessions()

        assert "data: [DONE]" in measured["result"], "流没有跑到终止帧，本用例没覆盖完整路径"
        assert measured["heartbeats"], "流式路径上的同步 DB 段期间事件循环一次都没被调度"
        _assert_every_session_stayed_on_its_worker_thread(sessions, measured["loop_thread"])

        db = factory()
        try:
            # 正向对照：轨迹行真的落到了库、终态是 done，否则上面的线程断言可能只是没走到写回
            row = db.query(ChatTraceSession).order_by(ChatTraceSession.created_at.desc()).first()
            assert row is not None, "整条流跑完却没有轨迹行，轨迹链没被本用例覆盖"
            assert row.status == "done", f"轨迹终态不是 done：{row.status}"
            stages = [event["stage"] for event in json.loads(row.events)]
            assert stages[0] == "request_received"
            assert "first_content_chunk" in stages
            saved = db.query(Message).filter_by(role="assistant").all()
            assert [message.content for message in saved] == ["迟到30分钟以内罚款50元。"]
        finally:
            db.close()
    finally:
        engine.dispose()


def test_trace_teardown_on_client_disconnect_still_lands(monkeypatch):
    """客户端在流中途断开：收尾那一次写回仍要真的落库，且不回到事件循环线程上。

    `event_stream` 的 `finally` 里那次 `finish` 是断连路径唯一的终态写入；把它改成「发了
    不管」，或者把取消当成写失败吞掉，都会让这条轨迹永远停在 running。
    """
    engine, factory = _session_factory(monkeypatch, delay_seconds=0.02)
    _seed_user_and_knowledge_base(factory)
    _stub_stream_boundaries(monkeypatch, chunks=("第一段", "第二段", "第三段"))

    async def call():
        response = await chat_service.stream_chat(
            ChatRequest(question="迟到怎么罚款"), authorization="Bearer token"
        )
        iterator = response.body_iterator
        first = await iterator.__anext__()
        await iterator.aclose()          # 等价于客户端断开后 Starlette 关掉响应生成器
        return first

    try:
        measured = asyncio.run(_measure(call))
        sessions = _snapshot_sessions()
        assert "data:" in (measured["result"].decode() if isinstance(measured["result"], bytes) else measured["result"])

        db = factory()
        try:
            row = db.query(ChatTraceSession).order_by(ChatTraceSession.created_at.desc()).first()
            assert row is not None, "断连路径没有落下任何轨迹行"
            assert row.status == "failed", (
                f"断连后轨迹没有落到终态（仍是 {row.status}）：收尾那次写回没落下或被吞了"
            )
        finally:
            db.close()
        _assert_every_session_stayed_on_its_worker_thread(sessions, measured["loop_thread"])
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 4) 形态门禁：事件循环线程上的轨迹写回（自带 2 正 5 负自检，共 7 条）
# ---------------------------------------------------------------------------

_TRACE_WRITE_NAMES = {
    "_safe_trace_add",
    "_safe_trace_attach",
    "_safe_trace_finish",
    "_trace_add",
    "append_trace_event",
}
_TRACE_RECEIVERS = {"trace", "trace_recorder", "recorder"}
_TRACE_METHODS = {"add", "attach", "finish"}

_SCANNER_SELFTEST = [
    (
        "await trace.add（不应命中）",
        "async def f(trace):\n    await trace.add('s', 'fn')\n",
        False,
    ),
    (
        "裸 trace.add（应命中）",
        "async def f(trace):\n    trace.add('s', 'fn')\n",
        True,
    ),
    (
        "await asyncio.to_thread(append_trace_event)（不应命中）",
        "async def f():\n    await asyncio.to_thread(append_trace_event, 't', 's', 'fn')\n",
        False,
    ),
    (
        "裸 append_trace_event（应命中）",
        "async def f():\n    append_trace_event('t', 's', 'fn')\n",
        True,
    ),
    (
        "同步 def 内（不应命中）",
        "def f():\n    append_trace_event('t', 's', 'fn')\n",
        False,
    ),
    (
        "同步嵌套 def 作载荷（不应命中）",
        "async def f():\n    def _w():\n        append_trace_event('t', 's', 'fn')\n"
        "    await asyncio.to_thread(_w)\n",
        False,
    ),
    (
        "数据库 db.add 不是轨迹写回（不应命中）",
        "async def f(db):\n    db.add(row)\n",
        False,
    ),
]

_TRACE_SURFACES = (
    "service/chat_service.py",
    "rag/llm.py",
    "rag/retrieval.py",
    "rag/memory_service.py",
    "rag/ragas_eval.py",
)


def _trace_write_name(node):
    func = node.func
    if isinstance(func, ast.Name) and func.id in _TRACE_WRITE_NAMES:
        return func.id
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in _TRACE_RECEIVERS
        and func.attr in _TRACE_METHODS
    ):
        return f"{func.value.id}.{func.attr}"
    return None


def _nearest_function(node, parents):
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return current
        current = parents.get(current)
    return None


def _is_offloaded(node, parents):
    """调用点是否被 await 或被 `await asyncio.to_thread(...)` 承载。"""
    parent = parents.get(node)
    if isinstance(parent, ast.Await):
        return True
    if isinstance(parent, ast.Call):
        func = parent.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "to_thread" and isinstance(parents.get(parent), ast.Await):
            return True
    return False


def scan_loop_thread_trace_writes(source: str) -> list[tuple[str, int]]:
    """列出「字面写在 async def 体内、既没 await 也没交给工作线程」的轨迹写回调用。"""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _trace_write_name(node)
        if not name:
            continue
        if not isinstance(_nearest_function(node, parents), ast.AsyncFunctionDef):
            continue
        if _is_offloaded(node, parents):
            continue
        hits.append((name, node.lineno))
    return sorted(set(hits), key=lambda item: item[1])


def test_the_trace_scanner_still_has_discriminating_power():
    """自检先跑：上面那份 0 命中只有在扫描器分得清正负样本时才值得采信。"""
    for label, source, expected in _SCANNER_SELFTEST:
        assert bool(scan_loop_thread_trace_writes(source)) is expected, f"扫描器自检失败：{label}"


@pytest.mark.parametrize("relative_path", _TRACE_SURFACES)
def test_no_trace_writeback_on_the_event_loop_thread(relative_path):
    """五个模块里的轨迹写回都必须 await 或被 to_thread 承载，与 #187 的口径对齐。"""
    path = BACKEND_ROOT / relative_path
    source = path.read_text(encoding="utf-8")
    # 防「文件读错/读空导致 0 命中」这种空过：先要求真的扫到了协程
    assert "async def " in source and "trace" in source

    hits = scan_loop_thread_trace_writes(source)
    assert hits == [], (
        f"{relative_path} 里仍有跑在事件循环线程上的轨迹写回：\n"
        + "\n".join(f"    {name}:{line}" for name, line in hits)
    )


def test_trace_recorder_persists_only_from_sync_payloads():
    """`rag/learning_trace.py`：落库调用只允许出现在**同步**函数里（= 交给线程的载荷）。"""
    source = (BACKEND_ROOT / "rag/learning_trace.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    crud_calls = [
        (node.lineno, node.func.attr, _nearest_function(node, parents))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "crud_trace"
    ]
    assert crud_calls, "一个 crud_trace 调用都没扫到：文件读错或落库被搬走了，本用例是空跑"
    for line, name, owner in crud_calls:
        assert isinstance(owner, ast.FunctionDef) and not isinstance(owner, ast.AsyncFunctionDef), (
            f"learning_trace.py:{line} 的 crud_trace.{name} 出现在协程里——落库必须在交给工作线程的同步载荷中"
        )

    async_methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name in _TRACE_METHODS
    }
    assert async_methods == _TRACE_METHODS, (
        f"TraceRecorder 的写回方法必须是协程，实际是 {sorted(async_methods)}："
        "同步方法没法把写库交给工作线程，调用方也就没法 await 它"
    )


# ---------------------------------------------------------------------------
# 5) 取消语义：写回路径只吞 Exception，取消/关流必须原样上抛
# ---------------------------------------------------------------------------
#
# `learning_trace.py:189` 与 `trace_service.py:29` 都对外声明：包装层只吞 `Exception`，
# `CancelledError` / `GeneratorExit` 照常上抛，断连语义不变。这条声明在本节之前没有任何
# 用例承接——把 `rag/learning_trace.py::_safe_persist` 与
# `service/trace_service.py::_safe_trace_finish` 的 `except Exception` 分别改成
# `except BaseException`，上文的 16 例仍然全绿（两处变异存活）。本节按四层各钉一次：
#
#   * 单点（包装层）：`_safe_trace_*` 收到 BaseException 必须上抛——配 `Exception` 正对照，
#     证明用例不是「什么异常都放行」。
#   * 单点（recorder）：`_safe_persist` 的 `except Exception` 不得加宽；异常是从**工作线程**
#     经 `to_thread` 的 future 传回事件循环的，这条传递链本身也在被测范围内。
#   * 在途：取消正好落在「已经交给工作线程、还没跑完」的那次写回上——取消必须穿透，且那次
#     写入仍要在工作线程里跑完落库（`chat_service.py:1193-1195` 依赖这条性质）。
#   * 流式：真实 `stream_chat` + 真实 `TraceRecorder`，取消落在收尾写回的 `await` 上。


class _BoomTrace:
    """写回方法直接抛指定异常的替身：只替换 recorder，三个包装层跑真实实现。"""

    def __init__(self, exc: BaseException):
        self._exc = exc
        self.calls: list[str] = []

    async def add(self, *_args, **_kwargs):
        self.calls.append("add")
        raise self._exc

    async def attach(self, *_args, **_kwargs):
        self.calls.append("attach")
        raise self._exc

    async def finish(self, *_args, **_kwargs):
        self.calls.append("finish")
        raise self._exc


@pytest.mark.parametrize(
    "wrapper_name, exc_type",
    [
        (name, exc_type)
        for name in ("_safe_trace_add", "_safe_trace_attach", "_safe_trace_finish")
        for exc_type in (asyncio.CancelledError, GeneratorExit)
    ],
    ids=lambda value: getattr(value, "__name__", value),
)
def test_safe_trace_wrappers_let_cancellation_through(wrapper_name, exc_type):
    """三个包装层都只吞 `Exception`：取消/关流原样上抛，普通失败仍要吞掉。

    两个方向合在一条用例里是刻意的。只有「BaseException 上抛」时，把实现改成什么都不吞
    （`raise` 一切）也会全绿；只有正对照时，宿主把 `except Exception` 加宽成
    `except BaseException` 同样全绿——那正是 review 里 A/D 两条存活变异。
    """
    wrapper = getattr(trace_service, wrapper_name)

    async def call():
        # 正对照：轨迹后端出错不能影响主链路，Exception 必须被吞掉并给兜底返回值。
        assert await wrapper(_BoomTrace(RuntimeError("trace backend down")), "stage", "fn") in ({}, None)
        # 承重：取消语义不能被改写。
        boom = _BoomTrace(exc_type())
        with pytest.raises(exc_type):
            await wrapper(boom, "stage", "fn")
        assert boom.calls, "替身压根没被调用，本用例是空跑"

    asyncio.run(call())


def test_recorder_writeback_lets_cancellation_through(monkeypatch):
    """`TraceRecorder._safe_persist` 的 `except Exception` 不得加宽为 `BaseException`。

    异常由工作线程抛出、经 `to_thread` 的 future 传回事件循环——`CancelledError` 是
    `Exception` 之外最要紧的那一类（客户端断连就是任务取消），正是这条分支的分界线。

    本用例只用 `CancelledError`，**不是**漏了 `GeneratorExit`：后者的传递形状不同，
    往一个挂起在 await 上的协程里 throw `GeneratorExit` 会走生成器关闭协议，实测（Python
    3.10/3.11 一致）异常是在**调用方那一帧**冒出来的，根本不进 `_safe_persist` 的 try——
    把 `except Exception` 改成 `except BaseException` 它照样上抛，是个打不响的臂。
    `GeneratorExit` 由上面包装层那条用例与 `aclose()` 断连用例承接，那里能真正打到边界。
    """
    engine, _ = _session_factory(monkeypatch)
    try:
        def boom(exc_type):
            def _raise(*_args, **_kwargs):
                raise exc_type()
            return _raise

        # 正对照：普通写失败仍然只吞在轨迹这一侧（既有契约）
        monkeypatch.setattr(crud_trace, "persist_trace_session", boom(RuntimeError))
        recorder = TraceRecorder(user_id=7)

        async def control():
            assert await recorder.add("request_received", "stream_chat") != {}

        asyncio.run(control())

        # 承重：BaseException 必须穿过去
        monkeypatch.setattr(crud_trace, "persist_trace_session", boom(asyncio.CancelledError))

        async def call():
            with pytest.raises(asyncio.CancelledError):
                await recorder.add("input_normalized", "stream_chat")

        asyncio.run(call())
    finally:
        engine.dispose()


def test_cancelling_an_in_flight_writeback_surfaces_and_the_row_still_lands(monkeypatch):
    """取消落在**已经交给工作线程**的那次写回上：取消要穿透，写入仍要落库。

    两个断言缺一不可：只断「取消穿透」时，把写回改成同步调用（不进线程）也能过；只断
    「写入落库」时，包装层吞掉取消同样能过——而吞掉取消就等于把 ASGI 栈已经收到的断连
    信号抹掉。
    """
    engine, factory = _session_factory(monkeypatch)
    entry = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    real_persist = crud_trace.persist_trace_session

    def gated(trace_id, **kwargs):
        if kwargs.get("status") == "failed" and not entry.is_set():
            entry.set()
            release.wait(10)      # 卡在写回**内部**：此刻调用方 await 的正是这一次写回
            try:
                return real_persist(trace_id, **kwargs)
            finally:
                finished.set()
        return real_persist(trace_id, **kwargs)

    monkeypatch.setattr(crud_trace, "persist_trace_session", gated)
    recorder = TraceRecorder(user_id=7)

    async def call():
        await recorder.add("request_received", "stream_chat")
        task = asyncio.create_task(recorder.finish("failed", conversation_id="conv-201"))
        await asyncio.to_thread(entry.wait, 10)
        assert entry.is_set(), "写回没有进入在途状态，本用例是空跑"
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.to_thread(finished.wait, 10)
        assert finished.is_set(), "取消把已经交出去的写回弄丢了：它没有在工作线程里跑完"

    try:
        asyncio.run(call())
        db = factory()
        try:
            row = db.query(ChatTraceSession).filter_by(id=recorder.trace_id).first()
            assert row is not None, "取消之后那次写回没有落库"
            assert row.status == "failed", f"终态没写回去：{row.status}"
            assert row.conversation_id == "conv-201", "attach 的会话绑定没写回去"
        finally:
            db.close()
    finally:
        engine.dispose()


def test_cancelling_the_stream_while_the_trace_writeback_is_in_flight(monkeypatch):
    """真实流式路径：取消落在收尾写回的 `await` 上时，取消必须穿透到调用方。

    比 `aclose()` 那条断连用例更贴近真实断连：`aclose()` 把 `GeneratorExit` 抛在挂起的
    `yield` 上，收尾写回是在 finally 里**另起**的一次 await，取消并不会同时到达包装层；
    这里让取消与写回在途**同时**发生（先卡住写回，再从外部 `task.cancel()`），取消因此
    直接落进 `_safe_trace_finish` 的 await——包装层一旦加宽成 `except BaseException`，
    这次取消就被吞掉，任务会照常跑到 `[DONE]`。
    """
    engine, factory = _session_factory(monkeypatch)
    _seed_user_and_knowledge_base(factory)
    _stub_stream_boundaries(monkeypatch, chunks=("第一段", "第二段", "第三段"))

    entry = threading.Event()
    release = threading.Event()
    real_persist = crud_trace.persist_trace_session

    def gated(trace_id, **kwargs):
        result = real_persist(trace_id, **kwargs)   # 写回先落库，再把 await 卡住
        if kwargs.get("status") == "done" and not entry.is_set():
            entry.set()
            release.wait(10)
        return result

    monkeypatch.setattr(crud_trace, "persist_trace_session", gated)

    seen: list[str] = []
    real_finish = trace_service._safe_trace_finish

    async def spy(*args, **kwargs):
        try:
            return await real_finish(*args, **kwargs)
        except BaseException as exc:      # 记录后原样上抛：委托型探针，包装层跑的还是真实实现
            seen.append(type(exc).__name__)
            raise

    monkeypatch.setattr(trace_service, "_safe_trace_finish", spy)
    monkeypatch.setattr(chat_service, "_safe_trace_finish", spy)

    async def call():
        response = await chat_service.stream_chat(
            ChatRequest(question="迟到怎么罚款"), authorization="Bearer token"
        )
        iterator = response.body_iterator
        chunks = []

        async def consume():
            async for chunk in iterator:
                chunks.append(chunk)

        task = asyncio.create_task(consume())
        await asyncio.to_thread(entry.wait, 10)
        assert entry.is_set(), "收尾写回没有进入在途状态，本用例是空跑"
        task.cancel()
        await asyncio.sleep(0.05)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return chunks

    try:
        chunks = asyncio.run(call())

        assert chunks, "流一个 chunk 都没发出来，本用例没覆盖到流式路径"
        assert seen == ["CancelledError"], (
            f"取消没有落进包装层的 await（包装层看到的异常 = {seen}）："
            "本用例已退化成只测「任务被取消」，钉不住 `except Exception` 这条边界"
        )
        db = factory()
        try:
            row = db.query(ChatTraceSession).order_by(ChatTraceSession.created_at.desc()).first()
            assert row is not None, "取消时已经交出去的写回没有落库"
            assert row.status == "done", f"在途那一次写回没有落到终态：{row.status}"
        finally:
            db.close()
    finally:
        engine.dispose()
