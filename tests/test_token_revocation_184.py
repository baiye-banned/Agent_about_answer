"""登出 / 改密后已签发 token 的吊销语义（issue #184）。

修复前服务端没有任何「使已签发 token 失效」的能力：`logout` 只回 `{"message": "ok"}`，
`update_password` 换掉 `password_hash` 之后，改密前签发的 token 在剩余有效期（默认 24h）
内继续可用。典型受损路径是「口令泄漏 → 用户改密 → 先前的 token 仍在线」。

修复把吊销拆成两种粒度，两者都需要，缺一不可：

- `users.token_version`：用户级世代。改密时 +1，改密前签发的**全部** token 作废。
- `revoked_tokens`：单枚 token 的登记表（按 `jti`）。登出只吊销用来自登出的那一枚，
  同一用户的其他会话不受影响——登出若也用世代，手机上退出登录会把桌面端一起踢掉。

断言全部打在**受保护端点的响应状态码**上，而不是「吊销表里有没有这一行」：吊销集合写
了却没接进校验链时，后者照样是绿的。

另一个容易只修一半的地方是鉴权入口有两条：FastAPI 依赖注入的 `get_current_user`，以及
不经依赖注入、自己开会话的 `chat_service.stream_chat`（原先直接调用 `decode_token`）。
吊销判定只挂在其中一条上，另一条就仍然认旧 token，所以下面两条链各有对应用例。
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool, StaticPool

import config
from conftest import FakeKnowledgeBase
from database import session as db_session
from database.session import Base
from model.models import RevokedToken, User
from router import auth as auth_router
from router import user as user_router
from schema.schemas import ChatRequest
from service import auth_service, chat_service


OLD_PASSWORD = "Old-Password-2026"  # scan-secrets:allow in-memory test fixture password
NEW_PASSWORD = "New-Password-2026"  # scan-secrets:allow in-memory test fixture password
NEWEST_PASSWORD = "Newest-Password-2026"  # scan-secrets:allow in-memory test fixture password


def _app_over(session_factory) -> FastAPI:
    """挂在给定会话工厂上的真路由应用（auth + user）。

    每条 HTTP 路径与实际用例都指向**同一个**会话工厂：登出写下的吊销行必须对发起
    调用的那条链可见，否则用例测的就不是同一份状态。
    """
    app = FastAPI()
    app.include_router(auth_router.router)
    app.include_router(user_router.router)

    def _per_request_session():
        # 每个请求一个新会话，照生产的 get_db 语义；共用同一个会话会把提交跨请求串起来。
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[db_session.get_db] = _per_request_session
    return app


def _sqlite_engine(*, poolclass, **pool_kwargs):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=poolclass,
        **pool_kwargs,
    )
    Base.metadata.create_all(bind=engine, tables=[User.__table__, RevokedToken.__table__])
    return engine


@pytest.fixture()
def api():
    engine = _sqlite_engine(poolclass=StaticPool)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with session_factory() as seed:
        seed.add_all(
            [
                User(username="alice", password_hash=auth_service.pwd_context.hash(OLD_PASSWORD)),
                User(username="bob", password_hash=auth_service.pwd_context.hash(OLD_PASSWORD)),
            ]
        )
        seed.commit()

    try:
        yield SimpleNamespace(
            client=TestClient(_app_over(session_factory)), sessions=session_factory
        )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# helpers —— 每次都在真实路由上跑，不直接调用 service 函数
# ---------------------------------------------------------------------------


def _login(client, password, username="alice") -> str:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _profile_status(client, token: str) -> int:
    return client.get("/api/user/profile", headers=_headers(token)).status_code


def _change_password(client, token: str, old: str, new: str):
    return client.put(
        "/api/user/password",
        json={"old_password": old, "new_password": new},
        headers=_headers(token),
    )


def _logout(client, token: str):
    return client.post("/api/auth/logout", headers=_headers(token))


# ---------------------------------------------------------------------------
# 验收 1 + 4：改密使改密前的 token 在受保护端点上 401
# ---------------------------------------------------------------------------


def test_change_password_makes_the_token_issued_before_it_401(api):
    token = _login(api.client, OLD_PASSWORD)
    # 前提断言：改密之前这枚 token 确实能用；少了它，后面那条 401 可能只是因为 token
    # 从一开始就是坏的（例如登录没能带回 token），用例会变成假绿。
    assert _profile_status(api.client, token) == 200

    assert _change_password(api.client, token, OLD_PASSWORD, NEW_PASSWORD).status_code == 200

    assert _profile_status(api.client, token) == 401


def test_change_password_still_answers_the_negated_control(api):
    """issue 原文第 7 步：旧口令不能再登录——证明第 5 步确实改掉了口令。"""
    token = _login(api.client, OLD_PASSWORD)
    _change_password(api.client, token, OLD_PASSWORD, NEW_PASSWORD)

    response = api.client.post(
        "/api/auth/login", json={"username": "alice", "password": OLD_PASSWORD}
    )

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 验收 2：阳性对照 —— 修复不能把新会话一起吊销
# ---------------------------------------------------------------------------


def test_a_login_after_the_change_gets_a_usable_token(api):
    token = _login(api.client, OLD_PASSWORD)
    _change_password(api.client, token, OLD_PASSWORD, NEW_PASSWORD)

    fresh = _login(api.client, NEW_PASSWORD)

    assert fresh != token
    assert _profile_status(api.client, fresh) == 200


def test_another_users_session_is_unaffected_by_the_change(api):
    """防止实现把世代做成进程级/全局计数：改密只作废本人的会话。"""
    alice = _login(api.client, OLD_PASSWORD, "alice")
    bob = _login(api.client, OLD_PASSWORD, "bob")

    _change_password(api.client, alice, OLD_PASSWORD, NEW_PASSWORD)

    assert _profile_status(api.client, bob) == 200


def test_a_token_without_the_generation_claim_is_rejected(api):
    """改动上线时，部署前签发的 token 里没有 ver，一律重新登录。

    这是刻意的：把「没有 ver」当成世代 0 放行，等于把存量 token 排除在这次修复之外。
    """
    legacy = jwt.encode(
        {"sub": "alice", "exp": datetime.now(timezone.utc) + timedelta(minutes=60)},
        config.SECRET_KEY,
        algorithm=config.ALGORITHM,
    )

    assert _profile_status(api.client, legacy) == 401


# ---------------------------------------------------------------------------
# 验收 3：登出只吊销用来自登出的那一枚 token
# ---------------------------------------------------------------------------


def test_logout_revokes_the_token_it_was_called_with(api):
    token = _login(api.client, OLD_PASSWORD)
    assert _profile_status(api.client, token) == 200

    assert _logout(api.client, token).status_code == 200

    assert _profile_status(api.client, token) == 401


def test_logout_leaves_the_other_session_of_the_same_user_alone(api):
    """两个会话（同一用户、两次登录）——登出其中一个，另一个必须照常可用。"""
    phone = _login(api.client, OLD_PASSWORD)
    desktop = _login(api.client, OLD_PASSWORD)
    assert phone != desktop

    assert _logout(api.client, phone).status_code == 200

    assert _profile_status(api.client, phone) == 401
    assert _profile_status(api.client, desktop) == 200


def test_logout_is_not_repeatable_with_a_revoked_token(api):
    """第二次登出拿同一个 token 来，必须在鉴权就被拒——否则登出等于没接进校验链。"""
    token = _login(api.client, OLD_PASSWORD)
    assert _logout(api.client, token).status_code == 200

    assert _logout(api.client, token).status_code == 401


# ---------------------------------------------------------------------------
# 验收 5：世代必须每一轮都变，不能只在首次生效
# ---------------------------------------------------------------------------


def test_two_consecutive_password_changes_revoke_each_previous_generation(api):
    first = _login(api.client, OLD_PASSWORD)

    assert _change_password(api.client, first, OLD_PASSWORD, NEW_PASSWORD).status_code == 200
    assert _profile_status(api.client, first) == 401

    second = _login(api.client, NEW_PASSWORD)
    assert _profile_status(api.client, second) == 200

    assert _change_password(api.client, second, NEW_PASSWORD, NEWEST_PASSWORD).status_code == 200
    # 关键一步：上一枚**新** token 也必须失效。世代只递增一次的实现会在这里放行。
    assert _profile_status(api.client, second) == 401

    third = _login(api.client, NEWEST_PASSWORD)
    assert _profile_status(api.client, third) == 200


# ---------------------------------------------------------------------------
# 内部对抗：改密接口自己不能被旧 token 调用
# ---------------------------------------------------------------------------


def test_change_password_rejects_a_token_that_was_already_logged_out(api):
    """改密接口自身也走同一条鉴权链：登出后的 token 连改密都调不动。"""
    token = _login(api.client, OLD_PASSWORD)
    assert _logout(api.client, token).status_code == 200

    response = _change_password(api.client, token, OLD_PASSWORD, NEW_PASSWORD)

    assert response.status_code == 401
    # 顺带证否「口令已经被改掉了」这条替代解释：旧口令仍然能登录。
    assert _login(api.client, OLD_PASSWORD) is not None


def test_change_password_rejects_a_token_that_is_one_generation_old(api):
    token = _login(api.client, OLD_PASSWORD)
    _change_password(api.client, token, OLD_PASSWORD, NEW_PASSWORD)

    # 用上一代的 token 再改一次：必须在鉴权处就被拒，不能只看口令对不对。
    response = _change_password(api.client, token, NEW_PASSWORD, NEWEST_PASSWORD)

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 第二条鉴权链：流式聊天自己开会话，不走依赖注入
# ---------------------------------------------------------------------------


def _patch_streaming_boundaries(monkeypatch, sessions, trace_recorder_cls):
    """只换掉流式入口的外部协作者；鉴权链本身跑真实实现。"""
    monkeypatch.setattr(chat_service, "SessionLocal", sessions)
    monkeypatch.setattr(chat_service, "TraceRecorder", trace_recorder_cls)
    monkeypatch.setattr(
        chat_service, "resolve_knowledge_base", lambda db, kid, user_id: FakeKnowledgeBase()
    )


def _stream(question: str, token: str):
    return chat_service.stream_chat(
        ChatRequest(question=question), authorization=f"Bearer {token}"
    )


def test_streaming_route_rejects_a_token_revoked_by_logout(api, monkeypatch, trace_recorder_cls):
    _patch_streaming_boundaries(monkeypatch, api.sessions, trace_recorder_cls)
    token = _login(api.client, OLD_PASSWORD)
    assert _logout(api.client, token).status_code == 200

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_stream("迟到怎么处理", token))

    assert exc_info.value.status_code == 401


def test_streaming_route_rejects_a_token_from_before_the_password_change(
    api, monkeypatch, trace_recorder_cls
):
    _patch_streaming_boundaries(monkeypatch, api.sessions, trace_recorder_cls)
    token = _login(api.client, OLD_PASSWORD)
    _change_password(api.client, token, OLD_PASSWORD, NEW_PASSWORD)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_stream("迟到怎么处理", token))

    assert exc_info.value.status_code == 401


def test_streaming_route_lets_a_live_token_through(api, monkeypatch, trace_recorder_cls):
    """阳性对照：有效的 token 必须过得了这道门。

    空问题会在鉴权的**下一步**被拒成 400，所以「不是 401」正是「鉴权放行」的证据；
    若鉴权把有效 token 也挡了，这里会拿到 401。
    """
    _patch_streaming_boundaries(monkeypatch, api.sessions, trace_recorder_cls)
    token = _login(api.client, OLD_PASSWORD)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_stream("", token))

    assert exc_info.value.status_code == 400


def test_streaming_route_closes_its_session_when_auth_fails(monkeypatch, trace_recorder_cls):
    """鉴权失败也要把会话还回连接池。

    池容量 1、不许溢出：会话不关，第二次请求就会卡在取连接上——这条断言锁的是
    流式入口新增的那条 `db.close()` 路径（鉴权在拿到 user 之前就退出了）。
    """
    engine = _sqlite_engine(poolclass=QueuePool, pool_size=1, max_overflow=0, pool_timeout=1)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with sessions() as seed:
        seed.add(User(username="alice", password_hash=auth_service.pwd_context.hash(OLD_PASSWORD)))
        seed.commit()

    client = TestClient(_app_over(sessions))
    _patch_streaming_boundaries(monkeypatch, sessions, trace_recorder_cls)

    revoked = _login(client, OLD_PASSWORD)
    assert _logout(client, revoked).status_code == 200

    try:
        async def _scenario():
            for _ in range(2):
                # 第二次请求只有在第一次真的归还了连接时才能跑起来。
                with pytest.raises(HTTPException) as exc_info:
                    await _stream("迟到怎么处理", revoked)
                assert exc_info.value.status_code == 401

        asyncio.run(_scenario())
        assert engine.pool.checkedout() == 0, "鉴权失败路径没有归还连接，连接池会被逐步耗尽"
    finally:
        engine.dispose()
