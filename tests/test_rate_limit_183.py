"""回归（issue #183）：登录失败节流、SSE 聊天流并发上限、口令长度上限。

断言都打在**路由层的状态码/响应头**上，而不是限流器内部的计数器：换一种实现（令牌桶、
滑动窗口、Redis 计数器）只要行为对，这些用例就该继续绿；行为不对（计数没接进路由、
闸门排队而不是拒绝、槽不归还）就该红。

    [1] POST /api/auth/login   连续 N 次错误口令后，第 N+1 次不再是普通 401（429 + Retry-After）；
                               同文件阳性对照：阈值内的正确口令仍然 200，防「一撞就锁死」。
    [2] POST /api/chat/stream  并发超上限立刻 4xx；用可控的假上游挂起流，证明是「被拒」而不是
                               「排队等到超时」。流正常结束 / 抛异常 / 客户端断开都要归还并发槽，
                               否则一轮断连就把闸门焊死（第二条并发用例第二轮全被拒）。
    [3] 口令字段               超过 bcrypt 输入上限的口令 → 422，而不是走到口令比对再回 401。

限流状态是**进程级**的（`service/rate_limit.py`）：用例前后都清空，避免用例之间、以及与
本目录其它测试文件之间互相污染。
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import config
from conftest import FakeKnowledgeBase, FakeTraceRecorder
from database import session as db_session
from database.session import Base
from model.models import Conversation, Message, User
from router import auth as auth_router
from router import chat as chat_router
from router import user as user_router
from schema import schemas
from schema.schemas import ChatRequest
from service import auth_service, chat_service, rate_limit


ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / ".env.example"

# issue #183 验收 4 给的长度：200 个字符。
LONG_PASSWORD = "p" * 200
# 与 bcrypt 的输入上限对齐的边界值；下面有用例断言它和 schema 里的常量一致。
BCRYPT_PASSWORD_LENGTH = 72
ALICE_PASSWORD = "alice-pass-183"  # scan-secrets:allow in-memory test fixture password
BOUNDARY_PASSWORD = "b" * BCRYPT_PASSWORD_LENGTH  # scan-secrets:allow in-memory test fixture password


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_text(_type, _compiler, **_kwargs):
    """MySQL 的 LONGTEXT 在 sqlite 上按 TEXT 建表（与 test_knowledge_ownership 一致）。"""
    return "TEXT"


class FakeClock:
    """可控时钟：锁定期满用「推进时间」验证，而不是让用例真的等满一个窗口。"""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


# ---------------------------------------------------------------------------
# [1] 登录失败节流
# ---------------------------------------------------------------------------


@pytest.fixture()
def login_api():
    """真实 auth/user 路由 + 内存 SQLite：跑真正的 bcrypt 校验，只换数据库连接。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=[User.__table__])
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    db = TestingSession()
    db.add_all(
        [
            User(username="alice", password_hash=auth_service.pwd_context.hash(ALICE_PASSWORD)),
            User(username="boundary", password_hash=auth_service.pwd_context.hash(BOUNDARY_PASSWORD)),
            User(username="bob", password_hash=auth_service.pwd_context.hash(ALICE_PASSWORD)),
        ]
    )
    db.commit()

    app = FastAPI()
    app.include_router(auth_router.router)
    app.include_router(user_router.router)
    app.dependency_overrides[db_session.get_db] = lambda: db

    rate_limit.login_failures.clear()
    try:
        yield SimpleNamespace(
            client=TestClient(app),
            db=db,
            alice_token=auth_service.create_token("alice"),
        )
    finally:
        rate_limit.login_failures.clear()
        db.close()
        engine.dispose()


def _login(api, password, username="alice"):
    return api.client.post("/api/auth/login", json={"username": username, "password": password})


def test_repeated_wrong_passwords_are_throttled_with_retry_after(login_api):
    """验收 1：连续 N 次错误口令之后，第 N+1 次不能再是普通 401。"""
    limit = rate_limit.login_failures.max_failures

    for attempt in range(1, limit + 1):
        response = _login(login_api, "wrong-password")
        assert response.status_code == 401, (
            f"第 {attempt}/{limit} 次错误口令返回 {response.status_code}，阈值内不该被限流"
        )

    throttled = _login(login_api, "wrong-password")
    assert throttled.status_code == 429, (
        f"第 {limit + 1} 次错误口令仍是 {throttled.status_code}：失败计数没有接进登录路由"
    )
    retry_after = throttled.headers.get("Retry-After")
    assert retry_after is not None, "限流响应缺少 Retry-After，调用方无从知道要等多久"
    assert int(retry_after) >= 1, f"Retry-After={retry_after!r} 不是正数秒"
    assert throttled.json().get("detail"), "限流响应没有可读的 detail"


def test_correct_password_within_threshold_still_returns_200(login_api):
    """验收 1 的阳性对照：阈值内的正确口令必须仍然 200，限流不能做成「一撞就锁死」。"""
    limit = rate_limit.login_failures.max_failures
    for _ in range(limit - 1):
        assert _login(login_api, "wrong-password").status_code == 401

    ok = _login(login_api, ALICE_PASSWORD)
    assert ok.status_code == 200, f"阈值内的正确口令被限流挡下（{ok.status_code}），合法用户被误锁"
    assert ok.json().get("token"), "登录成功但没有签发 token"


def test_successful_login_resets_the_failure_counter(login_api):
    """登录成功要清零计数：偶发输错不该攒成锁定。"""
    limit = rate_limit.login_failures.max_failures
    for _ in range(limit - 1):
        assert _login(login_api, "wrong-password").status_code == 401
    assert _login(login_api, ALICE_PASSWORD).status_code == 200

    for attempt in range(1, limit):
        response = _login(login_api, "wrong-password")
        assert response.status_code == 401, (
            f"成功登录后的第 {attempt} 次错误口令返回 {response.status_code}：计数没有清零，被累计成了锁定"
        )


@pytest.mark.parametrize("max_failures", [1, 3])
def test_lockout_expires_without_a_permanent_lock(login_api, monkeypatch, max_failures):
    """验收 1：锁定必须到期自动恢复，不能把合法用户永久关在门外。"""
    clock = FakeClock()
    monkeypatch.setattr(
        rate_limit,
        "login_failures",
        rate_limit.FailureWindow(max_failures, window_seconds=60, clock=clock),
    )

    for _ in range(max_failures):
        assert _login(login_api, "wrong-password").status_code == 401
    assert _login(login_api, "wrong-password").status_code == 429

    clock.advance(61)
    recovered = _login(login_api, ALICE_PASSWORD)
    assert recovered.status_code == 200, "窗口滑出后仍被拒绝：这是永久锁定，不是限流"


def test_threshold_is_read_from_the_limiter_not_hardcoded_in_the_route(login_api, monkeypatch):
    """验收 5：阈值来自限流器实例（由 config 构造），不是写在路由里的字面量。"""
    monkeypatch.setattr(rate_limit, "login_failures", rate_limit.FailureWindow(2, 60))

    assert _login(login_api, "wrong-password").status_code == 401
    assert _login(login_api, "wrong-password").status_code == 401
    assert _login(login_api, "wrong-password").status_code == 429, (
        "把阈值改成 2 之后第 3 次仍是 401：说明生效的阈值是写死的，不是可配置的"
    )


def test_throttle_key_ignores_forwarded_headers():
    """限流键取对端地址而不是 X-Forwarded-For：那个头由客户端伪造，拿它做键等于把键交给攻击者。"""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/login",
        "headers": [(b"x-forwarded-for", b"203.0.113.7")],
        "client": ("10.0.0.9", 5555),
        "server": ("testserver", 80),
        "scheme": "http",
        "query_string": b"",
    }
    request = Request(scope)

    assert auth_service._login_throttle_key(request, "Alice") == ("alice", "10.0.0.9")


# ---------------------------------------------------------------------------
# [2] SSE 聊天流并发上限
# ---------------------------------------------------------------------------


def _boundary_patches(monkeypatch, session_factory):
    """只打桩真正的边界（鉴权/会话工厂/trace/知识库解析/检索门），其余跑真实实现。"""
    monkeypatch.setattr(chat_service, "SessionLocal", session_factory)
    monkeypatch.setattr(chat_service, "decode_token", lambda authorization: "alice")
    monkeypatch.setattr(chat_service, "TraceRecorder", FakeTraceRecorder)
    monkeypatch.setattr(chat_service, "resolve_knowledge_base", lambda db, kid, user_id: FakeKnowledgeBase())

    async def fake_build_effective_question(question, attachments):
        return question, {"status": "skipped"}

    async def fake_recent_memory_text(*_args, **_kwargs):
        return ""

    async def fake_decide_need_rag(*_args, **_kwargs):
        return {"need_rag": False, "route": "direct", "confidence": 1.0, "reason": "test"}

    async def fake_retrieve_knowledge(*_args, **_kwargs):
        return [], {}

    monkeypatch.setattr(chat_service, "_build_effective_question", fake_build_effective_question)
    monkeypatch.setattr(chat_service, "_build_recent_memory_text", fake_recent_memory_text)
    monkeypatch.setattr(chat_service, "decide_need_rag", fake_decide_need_rag)
    monkeypatch.setattr(chat_service, "retrieve_knowledge", fake_retrieve_knowledge)
    monkeypatch.setattr(chat_service, "_schedule_memory_summary_update", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "schedule_ragas_evaluation", lambda *args, **kwargs: None)
    FakeTraceRecorder.instances = []


@pytest.fixture()
def stream_api(monkeypatch, tmp_path):
    """真实 chat 路由 + 文件 SQLite（每个请求各拿一条连接）+ 假上游，并发上限压到 1。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'rate-limit-183.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[User.__table__, Conversation.__table__, Message.__table__],
    )
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    with session_factory() as seed:
        seed.add(User(id=7, username="alice", password_hash="x"))
        seed.commit()

    _boundary_patches(monkeypatch, session_factory)

    gate = rate_limit.ConcurrencyGate(1)
    monkeypatch.setattr(rate_limit, "chat_stream_slots", gate)

    app = FastAPI()
    app.include_router(chat_router.router)
    try:
        yield SimpleNamespace(app=app, gate=gate, engine=engine)
    finally:
        engine.dispose()


STREAM_HEADERS = {"Authorization": "Bearer token"}


def _hanging_upstream(monkeypatch, started, release):
    """假上游：进入后置位 started 并一直挂住，直到用例放行。"""

    async def fake_stream_rag_answer(*_args, **_kwargs):
        started.set()
        await release.wait()
        yield "答案"

    monkeypatch.setattr(chat_service, "stream_rag_answer", fake_stream_rag_answer)


def _normal_upstream(monkeypatch):
    async def fake_stream_rag_answer(*_args, **_kwargs):
        yield "答案"

    monkeypatch.setattr(chat_service, "stream_rag_answer", fake_stream_rag_answer)


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def test_concurrent_stream_over_limit_is_rejected_before_the_slot_frees(stream_api, monkeypatch):
    """验收 3：并发超上限的那一路立刻被拒，而不是排队等到上游超时；流结束后槽要还回来。"""

    async def _scenario():
        started, release = asyncio.Event(), asyncio.Event()
        _hanging_upstream(monkeypatch, started, release)
        payload = {"question": "第一次提问"}

        async with _client(stream_api.app) as client:
            first = asyncio.create_task(client.post("/api/chat/stream", json=payload, headers=STREAM_HEADERS))
            await asyncio.wait_for(started.wait(), timeout=5)  # 前提：第 1 路已经占住并发槽

            second = await asyncio.wait_for(
                client.post("/api/chat/stream", json=payload, headers=STREAM_HEADERS), timeout=5
            )
            # 第 2 路必须在上游放行之前就返回，否则它其实是在排队而不是被拒。
            assert not release.is_set(), "第 2 路一直排到第一路上游结束才返回：闸门在排队而不是拒绝"

            release.set()
            first_response = await asyncio.wait_for(first, timeout=5)
            third = await asyncio.wait_for(
                client.post("/api/chat/stream", json=payload, headers=STREAM_HEADERS), timeout=5
            )
            return first_response, second, third

    first_response, second, third = asyncio.run(_scenario())

    assert 400 <= second.status_code < 500, f"并发超限返回了 {second.status_code}，不是可读的 4xx"
    assert second.status_code == 429
    assert second.json().get("detail"), "并发超限的响应没有可读的说明"
    assert second.headers.get("Retry-After") is not None, "并发超限没有给出 Retry-After"
    assert first_response.status_code == 200, "被挂起的那一路本身不该被拒"
    assert "data: [DONE]" in first_response.text, "第一路没有跑完，槽的归还没有被真正验证"
    assert third.status_code == 200, "流结束后并发槽没有归还：后续请求被一直拒在门外"


def test_stream_exception_releases_the_slot(stream_api, monkeypatch):
    """上游抛异常也要归还并发槽——否则一次上游抖动就会把闸门永久占住。"""

    async def _scenario():
        started = asyncio.Event()
        calls = {"count": 0}

        async def exploding_upstream(*_args, **_kwargs):
            # 只有第一路炸：第二路要正常跑完，否则测到的是「又炸了一次」，不是「槽还回来了」。
            calls["count"] += 1
            if calls["count"] == 1:
                started.set()
                raise RuntimeError("上游连接中断")
            yield "答案"

        monkeypatch.setattr(chat_service, "stream_rag_answer", exploding_upstream)
        payload = {"question": "第一次提问"}

        try:
            # 响应已经开始，异常只能往上传；anyio 的任务组可能把它包进异常组，所以下面按
            # 「异常里带着假上游的标记」判，而不是按异常类型判。异常也可能推迟到关闭客户端
            # （结束这条被中断的响应流）时才抛出，故整个 with 都包在里面。
            async with _client(stream_api.app) as client:
                await asyncio.wait_for(
                    client.post("/api/chat/stream", json=payload, headers=STREAM_HEADERS), timeout=5
                )
        except BaseException as exc:
            failure = exc
        else:
            failure = None

        if not started.is_set():
            raise AssertionError("假上游没有被调用，本用例没覆盖到异常路径")

        async with _client(stream_api.app) as client:
            follow_up = await asyncio.wait_for(
                client.post("/api/chat/stream", json=payload, headers=STREAM_HEADERS), timeout=5
            )
        return failure, follow_up

    failure, follow_up = asyncio.run(_scenario())

    assert failure is not None, "流里的上游异常被吞掉了，异常路径没被覆盖"
    assert "上游连接中断" in repr(failure), f"抛出来的不是假上游的异常：{failure!r}"
    assert follow_up.status_code == 200, "上游异常之后并发槽没有归还，下一路直接被拒"


def test_failure_before_the_stream_starts_releases_the_slot(stream_api, monkeypatch):
    """流还没开始就失败（知识库解析抛错）也要归还并发槽：这条路径不经过生成器的 finally。"""

    async def _scenario():
        def exploding_resolve(*_args, **_kwargs):
            raise RuntimeError("知识库解析失败")

        monkeypatch.setattr(chat_service, "resolve_knowledge_base", exploding_resolve)
        _normal_upstream(monkeypatch)

        try:
            async with _client(stream_api.app) as client:
                await asyncio.wait_for(
                    client.post(
                        "/api/chat/stream",
                        json={"question": "第一次提问"},
                        headers=STREAM_HEADERS,
                    ),
                    timeout=5,
                )
        except BaseException as exc:
            failure = exc
        else:
            failure = None

        monkeypatch.setattr(
            chat_service, "resolve_knowledge_base", lambda db, kid, user_id: FakeKnowledgeBase()
        )
        async with _client(stream_api.app) as client:
            follow_up = await asyncio.wait_for(
                client.post(
                    "/api/chat/stream", json={"question": "第二次提问"}, headers=STREAM_HEADERS
                ),
                timeout=5,
            )
        return failure, follow_up

    failure, follow_up = asyncio.run(_scenario())

    assert failure is not None, "知识库解析抛出的异常被吞掉了，本用例没覆盖到「流开始前失败」"
    assert follow_up.status_code == 200, "流开始之前失败时并发槽没有归还（外层收尾漏了 release）"


def test_early_return_stream_releases_the_slot(stream_api, monkeypatch):
    """图片识别失败那条早退流也占着并发槽：它是另一条 return，不能只盯着主流的 finally。"""

    async def _scenario():
        async def failed_vision_question(question, attachments):
            return question, {"status": "failed", "error": "图片内容识别失败"}

        monkeypatch.setattr(chat_service, "_build_effective_question", failed_vision_question)
        _normal_upstream(monkeypatch)
        payload = {"question": "", "attachments": [{"object_key": "rag-chat/probe.png", "name": "probe.png"}]}

        async with _client(stream_api.app) as client:
            early = await asyncio.wait_for(
                client.post("/api/chat/stream", json=payload, headers=STREAM_HEADERS), timeout=5
            )
            follow_up = await asyncio.wait_for(
                client.post(
                    "/api/chat/stream", json={"question": "第二次提问"}, headers=STREAM_HEADERS
                ),
                timeout=5,
            )
        return early, follow_up

    early, follow_up = asyncio.run(_scenario())

    assert "data: [DONE]" in early.text, f"早退流本身没跑完，本用例没测到它的收尾：{early.text[:200]}"
    assert follow_up.status_code == 200, "早退流结束后并发槽没有归还，后续请求被一直拒在门外"


def test_client_disconnect_releases_the_slot(stream_api, monkeypatch):
    """客户端断开（GeneratorExit/CancelledError）也要归还并发槽，并与路由层行为对上。"""
    _normal_upstream(monkeypatch)
    gate = stream_api.gate

    async def _scenario():
        response = await chat_service.stream_chat(
            ChatRequest(question="第一次提问"), authorization="Bearer token"
        )
        iterator = response.body_iterator
        await iterator.__anext__()  # 前提：流已经起来，槽确实被占着
        in_flight_while_streaming = gate.in_flight
        await iterator.aclose()  # 客户端断开

        async with _client(stream_api.app) as client:
            follow_up = await asyncio.wait_for(
                client.post("/api/chat/stream", json={"question": "第二次提问"}, headers=STREAM_HEADERS),
                timeout=5,
            )
        return in_flight_while_streaming, gate.in_flight, follow_up

    in_flight_while_streaming, after_disconnect, follow_up = asyncio.run(_scenario())

    assert in_flight_while_streaming == 1, "流式请求没有占住并发槽，本用例测不出泄漏"
    assert after_disconnect == 0, "客户端断开后并发槽没有归还"
    assert follow_up.status_code == 200, "断开之后下一路被拒，说明槽留在了上一个请求手里"


# ---------------------------------------------------------------------------
# [3] 口令字段长度上限 + 验收 5 的配置守恒
# ---------------------------------------------------------------------------


def test_oversized_login_password_is_rejected_with_422(login_api):
    """验收 4：超长口令必须在进入路由之前被 422 拒绝（当前实现会返回 401）。"""
    response = _login(login_api, LONG_PASSWORD)

    assert response.status_code == 422, (
        f"超长口令走到口令比对并返回 {response.status_code}；bcrypt 只取前 72 字节，"
        "比它长的口令会被静默截断，不同的长口令可以互相登录"
    )


def test_oversized_new_password_is_rejected_with_422(login_api):
    """验收 4：改口令的新口令字段同样要有上限。"""
    response = login_api.client.put(
        "/api/user/password",
        json={"old_password": ALICE_PASSWORD, "new_password": LONG_PASSWORD},
        headers={"Authorization": f"Bearer {login_api.alice_token}"},
    )

    assert response.status_code == 422, f"超长新口令返回 {response.status_code}，没有被 422 拒绝"


def test_password_length_boundary_keeps_the_usable_ones(login_api):
    """阳性对照：上限设在 bcrypt 的长度上，正好等于上限的口令必须还能登录。"""
    at_limit = _login(login_api, BOUNDARY_PASSWORD, username="boundary")
    assert at_limit.status_code == 200, f"上限长度的口令被拒（{at_limit.status_code}），阈值定得过低"

    over_limit = _login(login_api, BOUNDARY_PASSWORD + "b", username="boundary")
    assert over_limit.status_code == 422, f"超过上限一个字符仍然走到口令比对：{over_limit.status_code}"


def test_password_field_limit_matches_bcrypt_input_size():
    """上限本身要对着 bcrypt 的输入长度，而不是随手取的一个数。"""
    assert schemas.PASSWORD_MAX_LENGTH == BCRYPT_PASSWORD_LENGTH


def _documented_env_example():
    documented = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        documented[key.strip()] = value.strip()
    return documented


def test_thresholds_are_configurable_with_safe_defaults():
    """验收 5：阈值可配置、给的是安全默认值，且 .env.example 与 config.py 守恒。"""
    documented = _documented_env_example()
    configured = {
        "LOGIN_RATE_LIMIT_MAX_FAILURES": config.LOGIN_RATE_LIMIT_MAX_FAILURES,
        "LOGIN_RATE_LIMIT_WINDOW_SECONDS": config.LOGIN_RATE_LIMIT_WINDOW_SECONDS,
        "CHAT_STREAM_MAX_CONCURRENCY": config.CHAT_STREAM_MAX_CONCURRENCY,
    }
    for key, value in configured.items():
        assert key in documented, f"{key} 由 config.py 读取，却不在 .env.example 里（模板漏项）"
        assert documented[key] == str(value), (
            f"{key}: .env.example={documented[key]!r} config 默认值={value!r}，两边已经漂移"
        )

    # 生效的阈值就是这三个配置（改配置即改行为，路由里没有第二套数字）。
    assert rate_limit.login_failures.max_failures == config.LOGIN_RATE_LIMIT_MAX_FAILURES
    assert rate_limit.login_failures.window_seconds == config.LOGIN_RATE_LIMIT_WINDOW_SECONDS
    assert rate_limit.chat_stream_slots.limit == config.CHAT_STREAM_MAX_CONCURRENCY

    # 「安全默认值」的可检查口径：失败次数与并发数都收了口，窗口不是形同虚设。
    assert 1 <= config.LOGIN_RATE_LIMIT_MAX_FAILURES <= 10
    assert config.LOGIN_RATE_LIMIT_WINDOW_SECONDS >= 60
    assert 1 <= config.CHAT_STREAM_MAX_CONCURRENCY <= 64
    assert json.dumps(configured)  # 配置项必须全部是可序列化的普通值（不是对象/可调用）


def test_failure_window_ignores_zero_and_negative_thresholds():
    """0/负数会被读成「永远拒绝」，对运维是陷阱：构造时就该收敛到至少 1。"""
    window = rate_limit.FailureWindow(0, 0)
    assert window.max_failures == 1
    assert window.window_seconds == 1
    assert rate_limit.ConcurrencyGate(0).limit == 1
