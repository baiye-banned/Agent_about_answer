"""issue #83 第 6 项：`except IntegrityError` 要按约束来源分流。

`knowledge_bases` 上不止「同一用户同名」这一条约束。修复前 create/rename 两条写路径
不区分来源，一律 rollback 后抛 400「知识库名称已存在」，于是：

- 用户拿到与真实原因无关的诊断（按提示改名会继续失败）；
- 同一批新增的全局 409 兜底（main.integrity_error_handler）在这两条路径上永远不可达。

这里在真实 SQLite 引擎上开 `PRAGMA foreign_keys=ON`，让请求用户的 user_id 在库中
不存在（模拟请求在途时用户行被并发删除），走真实路由与真实的全局兜底处理器。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import main
from database import session as db_session
from database.session import Base
from model.models import KnowledgeBase, RevokedToken, User
from router import knowledge as knowledge_router
from service import auth_service, knowledge_service


DUPLICATE_NAME_MESSAGE = "知识库名称已存在"


@pytest.fixture()
def api():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        # SQLite 默认不强制外键；issue 的最小复现正依赖它把 user_id 冲突暴露成 IntegrityError。
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine, tables=[User.__table__, RevokedToken.__table__, KnowledgeBase.__table__])
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()

    alice = User(username="alice", password_hash="x")
    db.add(alice)
    db.commit()

    # 只存在于 Python 侧、库里没有对应行的用户：写入时 user_id 外键必然失败。
    orphan = User(id=4242, username="orphan", password_hash="x")

    app = FastAPI()
    app.include_router(knowledge_router.router)
    # 注册真实的全局兜底处理器，而不是在测试里复刻一份。
    app.add_exception_handler(IntegrityError, main.integrity_error_handler)
    app.dependency_overrides[db_session.get_db] = lambda: db
    app.dependency_overrides[auth_service.get_current_user] = lambda: alice

    def act_as(user):
        app.dependency_overrides[auth_service.get_current_user] = lambda: user

    try:
        yield TestClient(app, raise_server_exceptions=False), db, alice, orphan, act_as
    finally:
        db.close()


def test_duplicate_name_race_still_returns_400_with_the_same_message(api, monkeypatch):
    """并发重名（预检查已通过、写入时才撞唯一键）仍翻译成 400，文案不变。"""
    client, db, alice, _orphan, _act_as = api
    db.add(KnowledgeBase(name="考勤制度", user_id=alice.id))
    db.commit()

    # 预检查返回 False，模拟「检查时还没有同名库」，写入时才撞上唯一键。
    monkeypatch.setattr(
        knowledge_service.crud_knowledge_base, "knowledge_base_name_exists", lambda *a, **k: False
    )

    response = client.post("/api/knowledge-bases", json={"name": "考勤制度"})

    assert response.status_code == 400
    assert response.json()["detail"] == DUPLICATE_NAME_MESSAGE


@pytest.mark.parametrize(
    "detail, expected",
    [
        # 当前 schema：MySQL 具名唯一键（8.0.19 起带表名前缀 / 之前不带表名）。
        ("(1062, \"Duplicate entry '考勤制度' for key 'uq_knowledge_bases_user_name'\")", True),
        ("(1062, \"Duplicate entry '考勤制度' for key 'knowledge_bases.uq_knowledge_bases_user_name'\")", True),
        # 当前 schema：SQLite 无具名键，按冲突列反推。
        ("UNIQUE constraint failed: knowledge_bases.user_id, knowledge_bases.name", True),
        # 历史 schema（9b34e8a 之前的 name 单列唯一）：迁移换索引失败时仍可能遇到。
        ("UNIQUE constraint failed: knowledge_bases.name", True),
        ("(1062, \"Duplicate entry '考勤制度' for key 'knowledge_bases.name'\")", True),
        ("(1062, \"Duplicate entry '考勤制度' for key 'name'\")", True),
        # 非重名约束：绝不能误判成大重名。
        ("(1452, 'Cannot add or update a child row: a foreign key constraint fails "
         "(`rag_system`.`knowledge_bases`, CONSTRAINT `knowledge_bases_ibfk_1` "
         "FOREIGN KEY (`user_id`) REFERENCES `users` (`id`))')", False),
        ("FOREIGN KEY constraint failed", False),
        ("(1062, \"Duplicate entry '1' for key 'PRIMARY'\")", False),
        ("NOT NULL constraint failed: knowledge_bases.name", False),
    ],
)
def test_name_conflict_markers_cover_real_backend_errors(detail, expected):
    """按真实后端报文逐条核对分流判据（SQLite 现状 + MySQL 新旧版本 + 历史 schema）。"""
    from service.knowledge_service import _is_knowledge_base_name_conflict

    exc = IntegrityError("INSERT INTO knowledge_bases ...", {}, Exception(detail))

    assert _is_knowledge_base_name_conflict(exc) is expected


def test_non_name_constraint_conflict_falls_through_to_the_global_409(api):
    """非重名约束冲突（user_id 外键失效）走全局兜底 409，不再谎称重名。

    先证红：修复前这里返回 400「知识库名称已存在」，全局 409 兜底不可达。
    """
    client, _db, _alice, orphan, act_as = api
    act_as(orphan)

    response = client.post("/api/knowledge-bases", json={"name": "孤儿库"})

    assert response.status_code == 409
    assert response.json()["detail"] == main.INTEGRITY_CONFLICT_MESSAGE


def test_non_name_constraint_conflict_on_rename_is_reraised_not_relabelled(api, monkeypatch):
    """rename 走同一条分流：非重名冲突重新抛出交给全局兜底，不再翻译成 400。"""
    _client, db, alice, _orphan, _act_as = api
    entry = KnowledgeBase(name="考勤制度", user_id=alice.id)
    db.add(entry)
    db.commit()

    def foreign_key_failure(*_args, **_kwargs):
        raise IntegrityError(
            "UPDATE knowledge_bases SET name=?",
            {},
            Exception("FOREIGN KEY constraint failed"),
        )

    monkeypatch.setattr(
        knowledge_service.crud_knowledge_base, "rename_knowledge_base", foreign_key_failure
    )

    with pytest.raises(IntegrityError):
        knowledge_service.rename_knowledge_base(
            entry.id,
            type("Body", (), {"name": "新名字"})(),
            user=alice,
            db=db,
        )


def test_duplicate_name_unique_violation_on_rename_still_returns_400(api, monkeypatch):
    """对照：真正的重名唯一键冲突在 rename 上仍翻译成 400，文案不变。"""
    _client, db, alice, _orphan, _act_as = api
    entry = KnowledgeBase(name="考勤制度", user_id=alice.id)
    db.add(entry)
    db.commit()

    def unique_violation(*_args, **_kwargs):
        raise IntegrityError(
            "UPDATE knowledge_bases SET name=?",
            {},
            Exception("UNIQUE constraint failed: knowledge_bases.user_id, knowledge_bases.name"),
        )

    monkeypatch.setattr(
        knowledge_service.crud_knowledge_base, "rename_knowledge_base", unique_violation
    )

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        knowledge_service.rename_knowledge_base(
            entry.id,
            type("Body", (), {"name": "新名字"})(),
            user=alice,
            db=db,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == DUPLICATE_NAME_MESSAGE
