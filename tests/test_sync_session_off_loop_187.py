"""回归（issue #187）：流式对话与记忆压缩的同步 DB 段必须离开事件循环线程。

三路证据，各自独立成立：

1. **心跳**：同一个事件循环里先跑起心跳协程，再 await 目标协程。同步 DB 段一旦落在循环
   线程上，整段时间里心跳一次都排不上（修复前这里是 0 次）。这是本仓为这一类缺陷已经确立
   的 oracle（`test_knowledge_service.py` 的入库用例、`test_retrieval.py` 的检索用例），
   比断言耗时稳。
2. **线程**：Session 自己记录「开 / 用 / 关」各发生在哪个线程上。三条记录缺一不可——
   只记「用」分不出会话是不是别处开好递过来的，只记「开」证明不了它真的在那个线程上
   被用过。这就是验收第 3 条「`db` 不得跨线程传递」的直接判据。
3. **形态**：AST 扫描 async def 体内「没有被 to_thread / run_ingest_step / run_in_executor
   包住的同步 DB sink」，两个模块都必须是 0（验收第 5 条）。扫描器自带 2 正 3 负自检，
   先证明工具有区分力再采信 0 命中。

注入的 sleep 是**放大器不是测量值**：本机内存 SQLite 一次查询是 µs 级，不加放大器时
「心跳没被独占」与「压根没跑」不可区分。与耗时、与方言无关的结论来自线程记录。
"""

import ast
import asyncio
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
import rag.memory_service as memory_service
from config import MEMORY_SUMMARY_MAX_CHARS, MEMORY_WINDOW_TURNS
from conftest import FakeTraceRecorder
from database.session import Base
from model.models import Conversation, KnowledgeBase, Message, User
from schema.schemas import ChatRequest
from service import chat_service


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_text(_type, _compiler, **_kwargs):
    """MySQL 的 LONGTEXT 在 sqlite 上按 TEXT 建表（与其它用例一致）。"""
    return "TEXT"


class _RecordingSession(SASession):
    """把「会话在哪个线程上被开、被用、被关」记下来的 Session。

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
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    _RecordingSession.reset(delay_seconds)
    factory = sessionmaker(bind=engine, class_=_RecordingSession, autocommit=False, autoflush=False)
    monkeypatch.setattr(chat_service, "SessionLocal", factory)
    monkeypatch.setattr(memory_service, "SessionLocal", factory)
    return engine, factory


def _silence_learning_trace(monkeypatch):
    """关掉学习轨迹并换掉 TraceRecorder。

    轨迹写入走的是 `crud/trace.py` 自己开的会话，既不经过本用例的会话工厂、也不在 issue #187
    的范围内；不关掉它，用例就会去连生产库，把「环境不可达」误读成「修复失败」。
    """
    monkeypatch.setattr(learning_trace, "LEARNING_TRACE_ENABLED", False)
    monkeypatch.setattr(chat_service, "TraceRecorder", FakeTraceRecorder)


def _seed_conversation(factory, *, summary="", summary_upto=0, turns=0):
    db = factory()
    try:
        conversation = Conversation(
            id="conv-187",
            user_id=7,
            knowledge_base_id=1,
            title="迟到问题",
            memory_summary=summary,
            memory_summary_upto_message_id=summary_upto,
        )
        db.add(conversation)
        for index in range(turns):
            db.add(
                Message(
                    conversation_id="conv-187",
                    role="user" if index % 2 == 0 else "assistant",
                    content=f"第{index}条消息：迟到怎么罚款",
                )
            )
        db.commit()
    finally:
        db.close()
    # 布场用的会话不算进被测行为
    _RecordingSession.instances = []


async def _measure(call, *, tick_seconds=0.01, settle_seconds=0.05):
    """跑起心跳协程再 await 目标协程，返回调用期间的心跳、耗时、循环线程和返回值。

    `settle_seconds` 先让心跳真的被调度过：否则「0 次」可能只是心跳还没开始，
    与被测代码无关——这是本文件唯一可能自欺的地方，所以先断言基线非零。
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


def _assert_no_session_left_the_worker_thread(loop_thread):
    """每个会话都必须「开 → 用 → 关」整段在同一个工作线程里完成。"""
    sessions = _RecordingSession.instances
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


# ---------------------------------------------------------------------------
# 记忆压缩的三条路径（验收第 1 条）
# ---------------------------------------------------------------------------


def test_memory_summary_compaction_leaves_the_event_loop_thread(monkeypatch):
    """`_maybe_compact_memory_summary`：读摘要与写回压缩结果都不在循环线程上。"""
    engine, factory = _session_factory(monkeypatch, delay_seconds=0.15)
    try:
        _silence_learning_trace(monkeypatch)
        _seed_conversation(factory, summary="摘要超限内容" * (MEMORY_SUMMARY_MAX_CHARS // 6))
        measured = asyncio.run(_measure(lambda: memory_service._maybe_compact_memory_summary("conv-187")))
    finally:
        engine.dispose()

    assert measured["heartbeats"], "长期摘要压缩期间事件循环一次都没被调度"
    assert measured["elapsed"] >= 0.15, "注入的放大器没有生效，本用例测不到阻塞"
    _assert_no_session_left_the_worker_thread(measured["loop_thread"])


def test_sliding_window_memory_update_leaves_the_event_loop_thread(monkeypatch):
    """`_update_memory_summary_from_sliding_window`：滑出窗口的轮次并进长期记忆时同理。"""
    engine, factory = _session_factory(monkeypatch, delay_seconds=0.15)
    try:
        _silence_learning_trace(monkeypatch)
        _seed_conversation(factory, turns=(MEMORY_WINDOW_TURNS + 2) * 2)
        measured = asyncio.run(
            _measure(lambda: memory_service._update_memory_summary_from_sliding_window("conv-187", "trace-187"))
        )
    finally:
        engine.dispose()

    assert measured["heartbeats"], "滑动窗口记忆更新期间事件循环一次都没被调度"
    assert measured["elapsed"] >= 0.15
    _assert_no_session_left_the_worker_thread(measured["loop_thread"])


def test_recent_memory_text_leaves_the_event_loop_thread(monkeypatch):
    """`_build_recent_memory_text`：这条查询在 SSE 主链路上，每次流式请求都会走。"""
    engine, factory = _session_factory(monkeypatch, delay_seconds=0.15)
    try:
        _silence_learning_trace(monkeypatch)
        _seed_conversation(factory, turns=2)
        measured = asyncio.run(
            _measure(
                lambda: memory_service._build_recent_memory_text(
                    "conv-187", current_message_id=99, summary_upto=0, trace_id="trace-187"
                )
            )
        )
    finally:
        engine.dispose()

    assert measured["heartbeats"], "最近窗口查询期间事件循环一次都没被调度"
    assert measured["elapsed"] >= 0.15
    _assert_no_session_left_the_worker_thread(measured["loop_thread"])


# ---------------------------------------------------------------------------
# 流式对话入口与流内写库（验收第 2 条）
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


def _stub_stream_boundaries(monkeypatch):
    """只打桩真正的边界（鉴权/模型/检索），会话工厂与记忆查询都跑真实实现。"""
    def real_query_authenticate(db, authorization):
        # 真实实现里这一步是「回查用户 + 世代 + 吊销登记」，都在这条会话上；
        # 替身保留同一条查询，只是不解释 token。
        return db.query(User).filter_by(username="alice").first()

    monkeypatch.setattr(chat_service, "authenticate", real_query_authenticate)

    async def fake_decide_need_rag(*_args, **_kwargs):
        return {"need_rag": True, "route": "rag", "confidence": 1.0, "source": "test", "reason": "needs retrieval"}

    async def fake_retrieve_knowledge(question, knowledge_base_id, db, trace_recorder=None):
        return [{"file_name": "制度.txt", "content": "迟到罚款", "file_id": 1, "chunk_id": "a"}], {"routes": []}

    async def fake_stream_rag_answer(*_args, **_kwargs):
        yield "迟到30分钟以内罚款50元。"

    monkeypatch.setattr(chat_service, "decide_need_rag", fake_decide_need_rag)
    monkeypatch.setattr(chat_service, "retrieve_knowledge", fake_retrieve_knowledge)
    monkeypatch.setattr(chat_service, "stream_rag_answer", fake_stream_rag_answer)
    monkeypatch.setattr(chat_service, "schedule_ragas_evaluation", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "_schedule_memory_summary_update", lambda *args, **kwargs: None)


def test_stream_chat_entry_does_its_db_work_off_the_event_loop_thread(monkeypatch):
    """入口的 `SessionLocal()` + `query(User)` 与随后的知识库解析都在工作线程上。

    空问题让流程在鉴权与知识库解析之后立刻以 400 收尾：这两段是验收第 2 条点名的入口查询，
    再往后就不是「入口」了。
    """
    engine, factory = _session_factory(monkeypatch, delay_seconds=0.15)
    try:
        _seed_user_and_knowledge_base(factory)
        _stub_stream_boundaries(monkeypatch)
        _silence_learning_trace(monkeypatch)

        async def call():
            with pytest.raises(chat_service.HTTPException) as exc_info:
                await chat_service.stream_chat(ChatRequest(question=""), authorization="Bearer token")
            return exc_info.value.status_code

        measured = asyncio.run(_measure(call))
    finally:
        engine.dispose()

    assert measured["result"] == 400, "空问题应当在入口就被拒，本用例没有走到预期的分支"
    assert measured["heartbeats"], "流式入口的两段同步 DB 工作期间事件循环一次都没被调度"
    assert measured["elapsed"] >= 0.3, "注入的放大器没有生效（入口应当有两段被放大）"
    _assert_no_session_left_the_worker_thread(measured["loop_thread"])


def test_stream_chat_full_stream_keeps_every_session_on_its_worker_thread(monkeypatch):
    """整条流跑完：读会话、写用户消息、写 assistant 消息各自「开 → 用 → 关」在同一线程。"""
    engine, factory = _session_factory(monkeypatch, delay_seconds=0.15)
    _seed_user_and_knowledge_base(factory)
    # 带上已存在的会话 id：这样连「复用当前用户自己的知识库绑定」那条懒加载分支一起跑到
    _seed_conversation(factory)
    _stub_stream_boundaries(monkeypatch)
    _silence_learning_trace(monkeypatch)

    async def call():
        response = await chat_service.stream_chat(
            ChatRequest(question="迟到怎么罚款", conversation_id="conv-187"), authorization="Bearer token"
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    try:
        measured = asyncio.run(_measure(call))

        assert "data: [DONE]" in measured["result"], "流没有跑到终止帧，本用例没覆盖完整路径"
        assert measured["heartbeats"], "流式路径上的同步 DB 段期间事件循环一次都没被调度"
        _assert_no_session_left_the_worker_thread(measured["loop_thread"])

        # 正向对照：assistant 消息真的落库了，否则上面「会话都被正常开关」可能只是没走到写库
        db = factory()
        try:
            saved = db.query(Message).filter_by(conversation_id="conv-187", role="assistant").all()
            assert [message.content for message in saved] == ["迟到30分钟以内罚款50元。"]
        finally:
            db.close()
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 异常路径：搬动之后会话还得关得回去
# ---------------------------------------------------------------------------


def test_each_sync_db_step_closes_its_session_when_it_raises(monkeypatch):
    """同步 DB 段抛错时，它自己的会话仍要在同一个工作线程上关掉。

    这是「把整段搬进工作线程」引入的新风险：早先会话由外层 `try/except/finally` 统一收尾，
    搬进各段之后，收尾必须跟着搬进去——漏一条就是一个只有异常路径才暴露的连接泄漏。
    """
    engine, factory = _session_factory(monkeypatch)
    try:
        _silence_learning_trace(monkeypatch)
        _seed_user_and_knowledge_base(factory)

        def boom(db, knowledge_base_id, user_id):
            raise RuntimeError("知识库解析失败")

        monkeypatch.setattr(chat_service, "resolve_knowledge_base", boom)

        async def call():
            def real_query_authenticate(db, authorization):
                return db.query(User).filter_by(username="alice").first()

            monkeypatch.setattr(chat_service, "authenticate", real_query_authenticate)
            with pytest.raises(RuntimeError):
                await chat_service.stream_chat(
                    ChatRequest(question="迟到怎么罚款"), authorization="Bearer token"
                )

        measured = asyncio.run(_measure(call))
    finally:
        engine.dispose()

    assert measured["result"] is None
    _assert_no_session_left_the_worker_thread(measured["loop_thread"])


def test_a_failing_write_step_leaves_no_open_session(monkeypatch):
    """写库那一段失败（附件消费抛错）时会话同样要关掉，且不能把半截事务提交上去。"""
    engine, factory = _session_factory(monkeypatch)
    _seed_user_and_knowledge_base(factory)
    _stub_stream_boundaries(monkeypatch)
    _silence_learning_trace(monkeypatch)

    def boom(db, object_keys, user_id):
        raise RuntimeError("附件消费失败")

    monkeypatch.setattr(chat_service.crud_chat, "confirm_attachment_uploads", boom)

    async def call():
        with pytest.raises(RuntimeError):
            await chat_service.stream_chat(
                ChatRequest(question="迟到怎么罚款"), authorization="Bearer token"
            )

    try:
        asyncio.run(call())

        sessions = _RecordingSession.instances
        assert sessions
        assert all(session.closed_thread == session.opened_thread for session in sessions)
        db = factory()
        try:
            # 半截事务不能留下用户消息：写库那一段抛错后整个事务应当被回滚
            assert db.query(Message).filter_by(conversation_id="conv-187", role="user").all() == []
        finally:
            db.close()
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 形态门禁（验收第 5 条）
# ---------------------------------------------------------------------------

_WRAPPERS = {"to_thread", "run_ingest_step", "run_in_executor"}
_SESSION_NAMES = {"db", "session", "sess"}
_DB_METHODS = {"query", "commit", "close", "add", "flush", "delete", "execute", "rollback", "refresh"}

# 2 正 3 负：先证明扫描器有区分力，再采信后面的 0 命中
_SCANNER_SELFTEST = [
    ("裸 db.query（应命中）", "async def f(db):\n    db.query(X).first()\n", True),
    ("裸 SessionLocal（应命中）", "async def f():\n    db = SessionLocal()\n", True),
    (
        "to_thread(lambda) 包住（不应命中）",
        "async def f(db):\n    await asyncio.to_thread(lambda: db.query(X).first())\n",
        False,
    ),
    (
        "嵌套 def 作载荷（不应命中）",
        "async def f(db):\n    def _load():\n        return db.query(X).first()\n    await asyncio.to_thread(_load)\n",
        False,
    ),
    ("同步 def 内（不应命中）", "def f(db):\n    db.query(X).first()\n", False),
]

_TARGET_MODULES = ("rag/memory_service.py", "service/chat_service.py")


def _is_wrapper_call(node):
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        return name in _WRAPPERS
    return False


def _sink_name(call):
    func = call.func
    if isinstance(func, ast.Name) and func.id == "SessionLocal":
        return "SessionLocal()"
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in _SESSION_NAMES:
        if func.attr in _DB_METHODS:
            return f"{func.value.id}.{func.attr}()"
    return None


def _collect(node, wrapped, owner, hits):
    for child in ast.iter_child_nodes(node):
        child_wrapped, child_owner = wrapped, owner
        if _is_wrapper_call(child):
            child_wrapped = True
        if isinstance(child, (ast.FunctionDef, ast.Lambda)):
            child_wrapped = True                      # 同步嵌套函数 = 要交给线程池的载荷
        if isinstance(child, ast.AsyncFunctionDef):
            child_owner, child_wrapped = child, False  # 新的协程边界
        if child_owner is not None and not child_wrapped and isinstance(child, ast.Call):
            sink = _sink_name(child)
            if sink:
                hits.append((child_owner.name, child.lineno, sink))
        _collect(child, child_wrapped, child_owner, hits)


def scan_unwrapped_sync_db_sinks(source: str) -> list[tuple[str, int, str]]:
    """列出「字面上写在 async def 体内、且没被线程化包装 / 同步嵌套 def 挡住」的同步 DB sink。"""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AsyncFunctionDef):
            _collect(node, False, node, hits)
    return sorted(set(hits), key=lambda item: (item[1], item[0], item[2]))


def test_the_scanner_still_has_discriminating_power():
    """自检先跑：上面那份 0 命中只有在扫描器分得清正负样本时才值得采信。"""
    for label, source, expected in _SCANNER_SELFTEST:
        assert bool(scan_unwrapped_sync_db_sinks(source)) is expected, f"扫描器自检失败：{label}"


@pytest.mark.parametrize("relative_path", _TARGET_MODULES)
def test_no_unwrapped_sync_db_sink_outside_the_event_loop_rule(relative_path):
    """两个模块都不该再有裸的同步 DB sink——与 `rag/retrieval.py` 的 0 命中对齐。"""
    path = BACKEND_ROOT / relative_path
    source = path.read_text(encoding="utf-8")
    # 防「文件读错/读空导致 0 命中」这种空过：先要求真的扫到了协程
    assert "async def stream_chat" in source or "async def _maybe_compact_memory_summary" in source

    hits = scan_unwrapped_sync_db_sinks(source)
    assert hits == [], (
        f"{relative_path} 里仍有未被线程化包住的同步 DB sink（事件循环线程上跑同步 IO）：\n"
        + "\n".join(f"    {name}:{line} {sink}" for name, line, sink in hits)
    )
