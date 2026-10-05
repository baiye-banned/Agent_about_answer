"""Markdown export uses real SQLite rows, fixed ID bounds and authenticated HTTP (#252)."""

from datetime import datetime
import json
import re
from types import SimpleNamespace
from urllib.parse import quote, unquote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from crud import chat as crud_chat
from database.session import Base, get_db
from model.models import Conversation, Message, RevokedToken, User
from router import chat as chat_router
from service import chat_export_service
from service.auth_service import create_token, get_current_user


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_text(_type, _compiler, **_kwargs):
    return "TEXT"


@pytest.fixture()
def api():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[User.__table__, RevokedToken.__table__, Conversation.__table__, Message.__table__],
    )
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    alice = User(username="alice", password_hash="x")
    bob = User(username="bob", password_hash="x")
    db.add_all([alice, bob])
    db.commit()
    own = Conversation(id="own", user_id=alice.id, title="采购流程")
    foreign = Conversation(id="foreign", user_id=bob.id, title="其他用户的会话")
    db.add_all([own, foreign])
    db.commit()
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(_conn, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append((statement, parameters))

    app = FastAPI()
    app.include_router(chat_router.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: alice
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(
                db=db, client=client, alice=alice, bob=bob, own=own,
                foreign=foreign, statements=statements,
            )
    finally:
        db.close()
        engine.dispose()


def _seed_messages(api, count, *, cid="own", step=1, start=1):
    ids = [start + index * step for index in range(count)]
    api.db.add_all([
        Message(
            id=mid, conversation_id=cid,
            role="user" if index % 2 == 0 else "assistant",
            content=f"MESSAGE:{mid}", created_at=datetime(2020, 1, 1),
        )
        for index, mid in enumerate(ids)
    ])
    api.db.commit()
    api.db.expunge_all()
    api.statements.clear()
    return ids


def _export(api, cid="own"):
    return api.client.get(f"/api/chat/conversations/{cid}/export")


@pytest.mark.parametrize("count", [0, 1, 200, 201, 450])
def test_export_reads_all_messages_exactly_once_in_id_order(api, count):
    expected = _seed_messages(api, count)
    response = _export(api)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert [int(value) for value in re.findall(r"^MESSAGE:(\d+)$", response.text, re.M)] == expected
    assert response.text.count("## user · 2020-01-01T00:00:00") == (count + 1) // 2
    assert response.text.count("## assistant · 2020-01-01T00:00:00") == count // 2
    if count == 0:
        assert response.text == "# 采购流程\n"

    batch_selects = [
        (sql, params) for sql, params in api.statements
        if "FROM messages" in sql and "max(" not in sql.lower()
    ]
    assert len(batch_selects) == ((count + 199) // 200 + 1 if count else 0)
    for sql, parameters in batch_selects:
        assert "ORDER BY messages.id ASC" in sql
        assert "LIMIT" in sql
        assert parameters[-2:] == (200, 0)
        assert "attachments" not in sql
        assert "retrieval_trace" not in sql
        assert "ragas_" not in sql


def test_id_gaps_and_same_second_do_not_drop_or_duplicate_messages(api):
    ids = _seed_messages(api, 450, step=3)
    _seed_messages(api, 2, cid="foreign", start=2000)
    response = _export(api)
    assert response.status_code == 200
    assert [int(value) for value in re.findall(r"^MESSAGE:(\d+)$", response.text, re.M)] == ids


def test_starting_upper_id_excludes_messages_added_before_batches(api, monkeypatch):
    expected = _seed_messages(api, 201)
    original = crud_chat.get_message_export_upper_id

    def _capture_then_add(db, cid):
        upper_id = original(db, cid)
        db.add(Message(conversation_id=cid, role="user", content="ADDED_DURING_EXPORT"))
        db.commit()
        return upper_id

    monkeypatch.setattr(crud_chat, "get_message_export_upper_id", _capture_then_add)
    response = _export(api)
    assert response.status_code == 200
    assert "ADDED_DURING_EXPORT" not in response.text
    assert [int(value) for value in re.findall(r"^MESSAGE:(\d+)$", response.text, re.M)] == expected
    assert api.db.query(Message).filter_by(conversation_id="own").count() == 202


@pytest.mark.parametrize("cid", ["foreign", "missing"])
def test_foreign_and_missing_conversations_have_same_404(api, cid):
    _seed_messages(api, 1, cid="foreign")
    response = _export(api, cid)
    assert response.status_code == 404
    assert response.json() == {"detail": "对话不存在"}
    assert "其他用户" not in response.text
    assert not any("FROM messages" in sql for sql, _params in api.statements)


def test_export_requires_valid_jwt_and_uses_the_authenticated_owner(api):
    app = FastAPI()
    app.include_router(chat_router.router)
    app.dependency_overrides[get_db] = lambda: api.db
    with TestClient(app) as client:
        path = "/api/chat/conversations/own/export"
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer invalid"}).status_code == 401
        assert client.get(path, headers={"Authorization": f"Bearer {create_token('alice')}"}).status_code == 200
        response = client.get(path, headers={"Authorization": f"Bearer {create_token('bob')}"})
        assert response.status_code == 404
        assert response.json() == {"detail": "对话不存在"}


def test_export_preserves_full_content_and_source_fields_without_extra_data(api):
    body = "回答：中文 **Markdown**\n\n```python\nprint('完整正文')\n```\n" + "正文" * 1000
    source_content = "参考片段第一行\n第二行\n" + "来源" * 1000
    sources = [{
        "index": 1, "file_id": 37, "file_name": "采购制度.pdf", "chunk_id": "piece-4",
        "content": source_content, "excerpt": "截断的摘要",
        "route": "PRIVATE_ROUTE", "rerank_reason": "PRIVATE_REASON", "unexpected": "PRIVATE_EXTRA",
    }]
    api.db.add(Message(
        conversation_id="own", role="assistant", created_at=datetime(2020, 1, 1), content=body,
        sources=json.dumps(sources, ensure_ascii=False),
        attachments='[{"object_key":"PRIVATE_OBJECT","url":"PRIVATE_URL"}]',
        retrieval_trace='{"trace_id":"PRIVATE_TRACE"}', ragas_error="PRIVATE_ERROR",
    ))
    api.db.commit()
    response = _export(api)
    assert response.status_code == 200
    assert body in response.text
    assert source_content in response.text
    assert "[1] 采购制度.pdf（文件 ID：37；片段 ID：piece-4）" in response.text
    assert "PRIVATE_" not in response.text
    assert "截断的摘要" not in response.text


@pytest.mark.parametrize(
    "sources", ["", "not-json", "{}", "null", "123", '"text"', '[null, "bad", 3, [1]]']
)
def test_invalid_or_empty_sources_do_not_break_saved_content(api, sources):
    api.db.add(Message(conversation_id="own", role="assistant", content="完整正文", sources=sources))
    api.db.commit()
    response = _export(api)
    assert response.status_code == 200
    assert "完整正文" in response.text
    assert "### 参考来源" not in response.text


def test_historical_source_excerpt_is_used_when_full_content_is_missing(api):
    api.db.add(Message(
        conversation_id="own", role="assistant", content="答复",
        sources=json.dumps([{"file_name": "历史.txt", "excerpt": "已保存的历史片段"}], ensure_ascii=False),
    ))
    api.db.commit()
    response = _export(api)
    assert response.status_code == 200
    assert "历史.txt" in response.text
    assert "已保存的历史片段" in response.text


def test_user_message_sources_are_not_exported(api):
    api.db.add(Message(
        conversation_id="own", role="user", content="问题",
        sources='[{"file_name":"DO_NOT_EXPORT","content":"DO_NOT_EXPORT"}]',
    ))
    api.db.commit()
    response = _export(api)
    assert response.status_code == 200
    assert "DO_NOT_EXPORT" not in response.text


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("采购/报销：问题？", "采购报销：问题？.md"),
        ('../采购\\问题:\"<>|?*\r\n', "采购问题.md"),
        ("", "conversation.md"),
        ("  .<>:/\\|?*\x00\r\n  ", "conversation.md"),
        ("CON", "_CON.md"),
        ("nul.txt", "_nul.txt.md"),
        ("LPT9", "_LPT9.md"),
        ("正常 ' title", "正常 ' title.md"),
    ],
)
def test_safe_filename_header_preserves_chinese_and_blocks_control_characters(api, title, expected):
    api.own.title = title
    api.db.commit()
    response = _export(api)
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition == f"attachment; filename=\"conversation.md\"; filename*=UTF-8''{quote(expected, safe='')}"
    assert unquote(disposition.split("filename*=UTF-8''", 1)[1]) == expected
    assert "\r" not in disposition and "\n" not in disposition
    assert "\x00" not in disposition
    assert response.text == f"# {title}\n"


def test_very_long_filename_keeps_a_valid_utf8_boundary(api):
    api.own.title = "中" * 200
    api.db.commit()
    response = _export(api)
    assert response.status_code == 200
    name = unquote(response.headers["content-disposition"].split("filename*=UTF-8''", 1)[1])
    assert name == "中" * 60 + ".md"
    assert len(name.encode("utf-8")) < 255
    assert response.text == "# " + "中" * 200 + "\n"


def test_later_batch_failure_returns_no_partial_download_or_exception_text(api, monkeypatch, caplog):
    _seed_messages(api, 450)
    original = crud_chat.list_message_export_batch
    calls = 0

    def _fail_second_batch(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("PRIVATE_DATABASE_PASSWORD and PRIVATE_PATH")
        return original(*args, **kwargs)

    monkeypatch.setattr(crud_chat, "list_message_export_batch", _fail_second_batch)
    response = _export(api)
    assert response.status_code == 500
    assert response.json() == {"detail": chat_export_service.EXPORT_FAILED_MESSAGE}
    assert "content-disposition" not in response.headers
    assert "MESSAGE:" not in response.text
    assert "PRIVATE_" not in response.text
    assert "PRIVATE_" not in caplog.text
    assert "conversation Markdown export failed" in caplog.text


def test_export_is_read_only(api):
    _seed_messages(api, 201)
    before = [(row.id, row.content) for row in api.db.query(Message).order_by(Message.id).all()]
    api.statements.clear()
    assert _export(api).status_code == 200
    after = [(row.id, row.content) for row in api.db.query(Message).order_by(Message.id).all()]
    assert after == before
    assert api.db.get(Conversation, "own").title == "采购流程"
