"""消息级反馈持久化（issue #250）：赞 / 踩要落库，不是只在前端亮一下。

修复前没有任何地方记住单条回答的人工评价：用户点了赞/踩，刷新页面就没了。
本文件钉住这层契约：

- `POST /api/chat/messages/{id}/feedback` 把 `messages.feedback` 写成 1 / -1 / 0
  （1=赞、-1=踩、0=未反馈），消息历史接口再把该值原样带回；
- 幂等：同一条消息重复提交只覆盖 `feedback` 列，不新增行、不改 `created_at`；
- 归属与目标校验沿用全仓约定——查无此消息或属主不是我，一律 404，且不回显消息正文；
- 取值范围是闭集，非法值由 schema 统一产出 422，不靠 service 层手抛；
- 存量库没有 `feedback` 列，启动期补列迁移必须幂等地补上，旧行落成 0（=未反馈），
  与新建消息的默认值一致，因此不需要回填脚本。
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import session as db_session
from database.session import Base
from model.models import Conversation, Message, User
from router import chat as chat_router
from service import auth_service


# messages.content / retrieval_trace 用 MySQL 的 LONGTEXT，SQLite 渲染不出来；
# 建表时按 TEXT 编译，语义一致（与 tests/test_list_query_counts.py 同款）。
@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_text(_type, _compiler, **_kwargs):
    return "TEXT"


@pytest.fixture()
def api():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            Conversation.__table__,
            Message.__table__,
        ],
    )
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()

    alice = User(username="alice", password_hash="x")
    bob = User(username="bob", password_hash="x")
    db.add_all([alice, bob])
    db.commit()

    app = FastAPI()
    app.include_router(chat_router.router)
    app.dependency_overrides[db_session.get_db] = lambda: db
    # 归属不是每个用例的主题，默认固定为 alice；需要换人的用例用 act_as() 覆盖。
    app.dependency_overrides[auth_service.get_current_user] = lambda: alice

    def act_as(user):
        app.dependency_overrides[auth_service.get_current_user] = lambda: user

    try:
        yield SimpleNamespace(client=TestClient(app), db=db, alice=alice, bob=bob, act_as=act_as)
    finally:
        db.close()


def _add_conversation(api, cid):
    conversation = Conversation(id=cid, user_id=api.alice.id, title=f"会话-{cid}")
    api.db.add(conversation)
    api.db.commit()
    return conversation


def _add_message(api, cid, role, content):
    message = Message(conversation_id=cid, role=role, content=content, sources="[]", attachments="[]")
    api.db.add(message)
    api.db.commit()
    api.db.refresh(message)
    return message.id


def _history_row(api, cid, message_id):
    body = api.client.get(f"/api/chat/conversations/{cid}").json()
    return next(item for item in body if item["id"] == message_id)


def test_feedback_persists_and_comes_back_in_history(api):
    _add_conversation(api, "conv-persist")
    target = _add_message(api, "conv-persist", "assistant", "回答内容")

    response = api.client.post(f"/api/chat/messages/{target}/feedback", json={"feedback": 1})

    assert response.status_code == 200
    assert response.json() == {"message_id": target, "feedback": 1}
    # 落库而不是只改内存：重新查一次历史，同一个值要能读回来。
    assert _history_row(api, "conv-persist", target)["feedback"] == 1


def test_feedback_revalue_is_idempotent_and_does_not_touch_the_row(api):
    _add_conversation(api, "conv-revalue")
    target = _add_message(api, "conv-revalue", "assistant", "回答内容")
    created_at = _history_row(api, "conv-revalue", target)["created_at"]
    rows_before = api.db.query(Message).count()

    # 赞 → 踩 → 赞：每一步都成功，且都是「覆盖」而不是追加。
    for value in (1, -1, 1):
        response = api.client.post(f"/api/chat/messages/{target}/feedback", json={"feedback": value})
        assert response.status_code == 200
        assert response.json()["feedback"] == value

    row = _history_row(api, "conv-revalue", target)
    assert row["feedback"] == 1
    assert row["created_at"] == created_at  # 只覆盖反馈列，不动行的时间戳
    assert api.db.query(Message).count() == rows_before  # 不新增行


def test_feedback_on_someone_elses_message_is_404(api):
    _add_conversation(api, "conv-alice")
    target = _add_message(api, "conv-alice", "assistant", "回答内容")

    api.act_as(api.bob)
    response = api.client.post(f"/api/chat/messages/{target}/feedback", json={"feedback": 1})

    # 归属校验随查询一起做：别人的消息在 bob 眼里根本不存在，也不回显正文。
    assert response.status_code == 404
    assert "回答内容" not in response.text

    api.db.expunge_all()
    assert api.db.query(Message).filter_by(id=target).first().feedback == 0  # 未被改写


def test_feedback_on_unknown_message_is_404(api):
    response = api.client.post("/api/chat/messages/999999/feedback", json={"feedback": 1})
    assert response.status_code == 404


def test_illegal_feedback_values_are_rejected_by_the_schema(api):
    """取值范围是闭集 {-1, 0, 1}：越界值由 schema 统一产出 422，不靠 service 层手抛。"""
    _add_conversation(api, "conv-illegal")
    target = _add_message(api, "conv-illegal", "assistant", "回答内容")
    url = f"/api/chat/messages/{target}/feedback"

    # 2 越界、字符串不做隐式转换、None 不是「未反馈」（未反馈用 0 表示）。
    for value in (2, "1", None):
        response = api.client.post(url, json={"feedback": value})
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail[0]["type"] == "literal_error"
        assert detail[0]["loc"] == ["body", "feedback"]

    # 字段缺失同样拦在建库之前。
    missing = api.client.post(url, json={})
    assert missing.status_code == 422
    assert missing.json()["detail"][0]["loc"] == ["body", "feedback"]


def test_feedback_on_a_user_message_is_rejected(api):
    """反馈针对的是「回答」，对提问点赞没有意义；这条守卫只在服务端拦，前端不给入口。"""
    _add_conversation(api, "conv-role")
    target = _add_message(api, "conv-role", "user", "提问内容")

    response = api.client.post(f"/api/chat/messages/{target}/feedback", json={"feedback": 1})

    assert response.status_code == 422


def test_messages_default_to_no_feedback(api):
    _add_conversation(api, "conv-default")
    target = _add_message(api, "conv-default", "assistant", "回答内容")

    assert _history_row(api, "conv-default", target)["feedback"] == 0


def test_startup_migration_adds_the_feedback_column(monkeypatch, tmp_path):
    """老库升级：`init_db` 的补列迁移必须幂等地补上 `feedback`，存量行落成 0。

    0 恰好等于「未反馈」——也就是新列上线前所有历史消息的真实状态，语义上严格向后兼容，
    因此不需要回填脚本。
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE messages ("
            "id INTEGER NOT NULL PRIMARY KEY, "
            "conversation_id VARCHAR(36) NOT NULL, "
            "role VARCHAR(10) NOT NULL, "
            "content TEXT NOT NULL, "
            "sources TEXT, "
            "attachments TEXT, "
            "ragas_status VARCHAR(20), "
            "ragas_scores TEXT, "
            "ragas_error TEXT, "
            "retrieval_trace TEXT, "
            "created_at DATETIME NOT NULL)"
        ))
    monkeypatch.setattr(db_session, "engine", engine)

    before = {column["name"] for column in inspect(engine).get_columns("messages")}
    assert "feedback" not in before

    db_session._ensure_schema_columns()
    after = {column["name"] for column in inspect(engine).get_columns("messages")}
    assert "feedback" in after

    # 幂等：`init_db` 每次启动都会跑一遍，重复执行既不能报错也不能重复加列。
    db_session._ensure_schema_columns()
    assert {column["name"] for column in inspect(engine).get_columns("messages")} == after

    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO messages (conversation_id, role, content, created_at) "
            "VALUES ('c1', 'assistant', '旧回答', '2026-01-01 00:00:00')"
        ))
        assert conn.execute(text("SELECT feedback FROM messages")).scalar() == 0
