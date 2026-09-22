"""issue #60 回归：创建/重命名知识库的「预检查 + 写入」竞态。

两个并发请求都可能在预检查时看到「名字可用」，但唯一约束
uq_knowledge_bases_user_name 只允许一方提交成功。败方的 IntegrityError 必须被
翻译成与串行路径一致的 400「知识库名称已存在」，并且失败会话要 rollback 到可继续使用，
而不是把失败事务状态留给后续逻辑。

用例用真实 SQLite 引擎 + 每个 Session 一条独立连接，在预检查与写入之间插入对手请求的
真实提交，复现的是同一个交错时序，不依赖线程调度的运气。

另外覆盖验收标准第 4 条：写路径没有单独处理的约束冲突由 `main` 上注册的全局兜底翻译成 4xx，
不再冒成 500。
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import main as main_module
from crud import knowledge_base as crud_knowledge_base
from database import session as db_session
from database.session import Base
from model.models import Conversation, KnowledgeBase, KnowledgeFile, RevokedToken, User
from router import knowledge as knowledge_router
from service import knowledge_service
from service.auth_service import get_current_user


DUPLICATE_NAME_DETAIL = "知识库名称已存在"


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_text(_type, _compiler, **_kwargs):
    return "TEXT"


@pytest.fixture()
def api(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'kb-name-race.db').as_posix()}",
        connect_args={"check_same_thread": False},
        # 每个 Session 一条独立连接：竞态用例要两个真实并发的会话，不能共享同一条连接。
        poolclass=NullPool,
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[User.__table__, RevokedToken.__table__, KnowledgeBase.__table__, KnowledgeFile.__table__, Conversation.__table__],
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    sessions = []
    issued = []

    def new_session():
        db = TestingSession()
        sessions.append(db)
        return db

    def new_request_session():
        db = new_session()
        issued.append(db)
        return db

    setup = new_session()
    user = User(username="alice", password_hash="x")
    setup.add(user)
    setup.commit()

    def override_get_db():
        # 故意不在收尾 close：用例要检查请求会话在冲突之后的真实事务状态。
        yield new_request_session()

    def override_current_user():
        return user

    app = FastAPI()
    app.include_router(knowledge_router.router)
    app.dependency_overrides[db_session.get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user

    # 本用例断言的是「接口不返回 500」，所以让未捕获异常走 500 响应而不是冒进测试进程。
    client = TestClient(app, raise_server_exceptions=False)

    try:
        yield SimpleNamespace(
            client=client,
            monkeypatch=monkeypatch,
            new_session=new_session,
            issued=issued,
            user=user,
        )
    finally:
        for db in sessions:
            db.close()
        engine.dispose()


def _seed_knowledge_bases(api, *names):
    db = api.new_session()
    created = []
    for name in names:
        created.append(crud_knowledge_base.create_knowledge_base(db, name, api.user.id))
    return created


def _stored_names(api):
    """库里当前存在的知识库名字（排序后比较，避免依赖自增 id 顺序）。"""
    db = api.new_session()
    return sorted(row.name for row in db.query(KnowledgeBase).all())


def _stale_precheck(api, name, commit_rival):
    """把预检查换成「真实查询 + 对手抢先提交」的交错时序。

    real_exists 是未打桩的原函数：先真实查询确认此刻确实看不到同名记录（复现请求 A 的预检查窗口），
    再让对手请求提交，最后返回 False —— 等价于 A 带着过期的预检查结论继续写入。
    """
    real_exists = crud_knowledge_base.knowledge_base_name_exists

    def stale_precheck(db, probe_name, user_id, exclude_id=None):
        assert probe_name == name
        assert real_exists(db, probe_name, user_id, exclude_id) is False
        commit_rival()
        return False

    api.monkeypatch.setattr(
        knowledge_service.crud_knowledge_base, "knowledge_base_name_exists", stale_precheck
    )


def _assert_session_usable_after_conflict(api):
    """冲突后请求会话必须已回滚：事务结束且能继续查询（未回滚会抛 PendingRollbackError）。"""
    request_db = api.issued[-1]
    assert request_db.in_transaction() is False
    assert request_db.query(KnowledgeBase).count() == len(_stored_names(api))


def test_create_knowledge_base_loses_race_returns_400_without_500(api):
    rival = api.new_session()
    _stale_precheck(api, "制度库", lambda: crud_knowledge_base.create_knowledge_base(rival, "制度库", api.user.id))

    response = api.client.post("/api/knowledge-bases", json={"name": "制度库"})

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == DUPLICATE_NAME_DETAIL
    # 只有对手那一条记录落库，败方没有写出重复行
    assert _stored_names(api) == ["制度库"]
    _assert_session_usable_after_conflict(api)


def test_rename_knowledge_base_loses_race_returns_400_without_500(api):
    target, rival = _seed_knowledge_bases(api, "制度库甲", "制度库乙")
    rival_db = api.new_session()
    _stale_precheck(
        api, "制度库", lambda: crud_knowledge_base.rename_knowledge_base(rival_db, rival.id, "制度库", api.user.id)
    )

    response = api.client.put(f"/api/knowledge-bases/{target.id}", json={"name": "制度库"})

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == DUPLICATE_NAME_DETAIL
    # 只有对手改名成功，败方仍保留原名
    assert _stored_names(api) == ["制度库", "制度库甲"]
    _assert_session_usable_after_conflict(api)


def test_serial_duplicate_name_keeps_the_same_400_message(api):
    _seed_knowledge_bases(api, "制度库")

    response = api.client.post("/api/knowledge-bases", json={"name": "制度库"})

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == DUPLICATE_NAME_DETAIL
    assert _stored_names(api) == ["制度库"]


def test_global_handler_translates_unhandled_integrity_error_to_4xx():
    """验收标准第 4 条：未被写路径单独处理的约束冲突不再冒成 500。

    真实应用上必须注册全局兜底；再用一个临时探针路由触发未捕获的 IntegrityError，
    断言拿到的是 409 + 可读文案，而不是 Starlette 默认的 500。
    """
    handler = main_module.app.exception_handlers.get(IntegrityError)
    assert handler is not None, "应用未注册全局 IntegrityError 兜底处理器"

    async def raise_integrity_error():
        raise IntegrityError(
            "INSERT INTO probe (id) VALUES (1)", {}, Exception("UNIQUE constraint failed: probe.id")
        )

    probe_app = FastAPI()
    probe_app.add_exception_handler(IntegrityError, handler)
    probe_app.add_api_route("/probe", raise_integrity_error, methods=["POST"])

    response = TestClient(probe_app, raise_server_exceptions=False).post("/probe")

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == main_module.INTEGRITY_CONFLICT_MESSAGE
