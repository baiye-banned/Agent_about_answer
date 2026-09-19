"""issue #9 回归：知识库与知识文件必须按归属用户过滤。

每个用例都让 bob 去访问 alice 的资源，期望得到 404（而不是 403），
以免通过状态码差异判断资源是否存在。
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import session as db_session
from database.session import Base
from model.models import Conversation, KnowledgeBase, KnowledgeFile, User
from router import auth as auth_router
from router import knowledge as knowledge_router
from service import auth_service, knowledge_service


ROOT = Path(__file__).resolve().parents[1]
ALICE_PASSWORD = "alice-pass"
BOB_PASSWORD = "bob-pass"
SECRET = "alice 的私有资料：2026 年报价单，bob 不应该看到。"
ALICE_PASSWORD_HASH = auth_service.pwd_context.hash(ALICE_PASSWORD)
BOB_PASSWORD_HASH = auth_service.pwd_context.hash(BOB_PASSWORD)


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_text(_type, _compiler, **_kwargs):
    return "TEXT"


@pytest.fixture()
def api(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            KnowledgeBase.__table__,
            KnowledgeFile.__table__,
            Conversation.__table__,
        ],
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    db = TestingSession()

    alice = User(username="alice", password_hash=ALICE_PASSWORD_HASH)
    bob = User(username="bob", password_hash=BOB_PASSWORD_HASH)
    db.add_all([alice, bob])
    db.commit()

    alice_base = KnowledgeBase(name="alice-private-kb")
    alice_base.user_id = alice.id
    db.add(alice_base)
    db.commit()

    alice_file = KnowledgeFile(
        knowledge_base_id=alice_base.id,
        name="alice-secret.txt",
        size=len(SECRET),
        content=SECRET,
    )
    alice_file.user_id = alice.id
    db.add(alice_file)
    db.commit()

    app = FastAPI()
    app.include_router(auth_router.router)
    app.include_router(knowledge_router.router)
    app.dependency_overrides[db_session.get_db] = lambda: db

    # 向量库不参与归属校验，用例里直接短路，避免连真实 Milvus。
    monkeypatch.setattr(knowledge_service, "add_chunks", lambda *args, **kwargs: None)
    monkeypatch.setattr(knowledge_service, "delete_file_chunks", lambda *args, **kwargs: None)

    client = TestClient(app)
    try:
        yield SimpleNamespace(
            client=client,
            db=db,
            alice=alice,
            bob=bob,
            alice_base=alice_base,
            alice_file=alice_file,
        )
    finally:
        db.close()


def _headers(client, username, password):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _alice_headers(api):
    return _headers(api.client, "alice", ALICE_PASSWORD)


def _bob_headers(api):
    return _headers(api.client, "bob", BOB_PASSWORD)


def test_other_user_does_not_see_foreign_knowledge_base(api):
    response = api.client.get("/api/knowledge-bases", headers=_bob_headers(api))

    assert response.status_code == 200
    names = [item["name"] for item in response.json()]
    assert "alice-private-kb" not in names
    assert all(item["id"] != api.alice_base.id for item in response.json())


def test_other_user_cannot_list_files_of_foreign_knowledge_base(api):
    response = api.client.get(
        "/api/knowledge",
        params={"knowledge_base_id": api.alice_base.id},
        headers=_bob_headers(api),
    )

    assert response.status_code == 404
    assert SECRET not in response.text


def test_other_user_cannot_read_foreign_file_detail(api):
    response = api.client.get(f"/api/knowledge/{api.alice_file.id}", headers=_bob_headers(api))

    assert response.status_code == 404
    assert "alice-secret.txt" not in response.text


def test_other_user_cannot_read_foreign_file_content(api):
    response = api.client.get(f"/api/knowledge/{api.alice_file.id}/content", headers=_bob_headers(api))

    assert response.status_code == 404
    assert SECRET not in response.text


def test_other_user_cannot_delete_foreign_file(api):
    response = api.client.delete(f"/api/knowledge/{api.alice_file.id}", headers=_bob_headers(api))

    assert response.status_code == 404
    api.db.expire_all()
    assert api.db.query(KnowledgeFile).filter_by(id=api.alice_file.id).first() is not None


def test_other_user_cannot_rename_foreign_knowledge_base(api):
    response = api.client.put(
        f"/api/knowledge-bases/{api.alice_base.id}",
        json={"name": "bob-took-over"},
        headers=_bob_headers(api),
    )

    assert response.status_code == 404
    api.db.expire_all()
    assert api.db.query(KnowledgeBase).filter_by(id=api.alice_base.id).first().name == "alice-private-kb"


def test_other_user_cannot_delete_foreign_knowledge_base(api):
    response = api.client.delete(f"/api/knowledge-bases/{api.alice_base.id}", headers=_bob_headers(api))

    assert response.status_code == 404
    api.db.expire_all()
    assert api.db.query(KnowledgeBase).filter_by(id=api.alice_base.id).first() is not None
    assert api.db.query(KnowledgeFile).filter_by(id=api.alice_file.id).first() is not None


def test_other_user_cannot_upload_into_foreign_knowledge_base(api):
    response = api.client.post(
        "/api/knowledge/upload",
        files={"file": ("bob-payload.txt", b"bob payload", "text/plain")},
        data={"knowledge_base_id": str(api.alice_base.id)},
        headers=_bob_headers(api),
    )

    assert response.status_code == 404
    api.db.expire_all()
    remaining = api.db.query(KnowledgeFile).filter_by(knowledge_base_id=api.alice_base.id).all()
    assert [entry.id for entry in remaining] == [api.alice_file.id]


def test_owner_can_still_manage_own_knowledge(api):
    headers = _alice_headers(api)

    bases = api.client.get("/api/knowledge-bases", headers=headers).json()
    assert [item["id"] for item in bases] == [api.alice_base.id]

    files = api.client.get(
        "/api/knowledge",
        params={"knowledge_base_id": api.alice_base.id},
        headers=headers,
    ).json()
    assert [item["id"] for item in files] == [api.alice_file.id]

    content = api.client.get(f"/api/knowledge/{api.alice_file.id}/content", headers=headers)
    assert content.status_code == 200
    assert content.json()["content"] == SECRET

    assert api.client.delete(f"/api/knowledge/{api.alice_file.id}", headers=headers).json() == {"message": "ok"}
    api.db.expire_all()
    assert api.db.query(KnowledgeFile).filter_by(id=api.alice_file.id).first() is None


def test_knowledge_base_name_is_unique_per_user(api):
    bob_headers = _bob_headers(api)
    payload = {"name": "alice-private-kb"}

    assert api.client.post("/api/knowledge-bases", json=payload, headers=bob_headers).status_code == 200
    assert api.client.post("/api/knowledge-bases", json=payload, headers=bob_headers).status_code == 400

    alice_headers = _alice_headers(api)
    assert api.client.post("/api/knowledge-bases", json=payload, headers=alice_headers).status_code == 400


def test_default_knowledge_base_is_created_per_user(api):
    assert api.client.get("/api/knowledge", headers=_alice_headers(api)).status_code == 200
    assert api.client.get("/api/knowledge", headers=_bob_headers(api)).status_code == 200

    api.db.expire_all()
    bases = api.db.query(KnowledgeBase).order_by(KnowledgeBase.id.asc()).all()
    # alice 已经有知识库就直接复用，bob 没有则新建一个属于他自己的默认知识库
    assert [(entry.name, entry.user_id) for entry in bases] == [
        ("alice-private-kb", api.alice.id),
        ("默认知识库", api.bob.id),
    ]


def test_two_users_can_each_own_a_default_knowledge_base(api):
    carol = User(username="carol", password_hash=ALICE_PASSWORD_HASH)
    dave = User(username="dave", password_hash=ALICE_PASSWORD_HASH)
    api.db.add_all([carol, dave])
    api.db.commit()

    for username in ("carol", "dave"):
        headers = _headers(api.client, username, ALICE_PASSWORD)
        assert api.client.get("/api/knowledge", headers=headers).status_code == 200

    api.db.expire_all()
    defaults = api.db.query(KnowledgeBase).filter_by(name="默认知识库").all()
    assert {entry.user_id for entry in defaults} == {carol.id, dave.id}


def test_legacy_rows_without_owner_are_invisible(api):
    legacy_base = KnowledgeBase(name="legacy-kb")
    api.db.add(legacy_base)
    api.db.commit()
    legacy_file = KnowledgeFile(
        knowledge_base_id=legacy_base.id,
        name="legacy.txt",
        size=1,
        content="legacy content",
    )
    api.db.add(legacy_file)
    api.db.commit()

    for headers in (_alice_headers(api), _bob_headers(api)):
        listing = api.client.get("/api/knowledge-bases", headers=headers).json()
        assert all(item["id"] != legacy_base.id for item in listing)
        assert api.client.get(
            "/api/knowledge",
            params={"knowledge_base_id": legacy_base.id},
            headers=headers,
        ).status_code == 404
        assert api.client.get(f"/api/knowledge/{legacy_file.id}/content", headers=headers).status_code == 404
        assert api.client.delete(f"/api/knowledge/{legacy_file.id}", headers=headers).status_code == 404


def test_chat_conversation_listing_hides_foreign_knowledge_base(api):
    from service import chat_service

    stolen = Conversation(user_id=api.bob.id, title="bob-stolen", knowledge_base_id=api.alice_base.id)
    own = Conversation(user_id=api.alice.id, title="alice-own", knowledge_base_id=api.alice_base.id)
    api.db.add_all([stolen, own])
    api.db.commit()

    alice_rows = {item["id"]: item for item in chat_service.list_conversations(user=api.alice, db=api.db)}
    bob_rows = {item["id"]: item for item in chat_service.list_conversations(user=api.bob, db=api.db)}

    assert set(alice_rows) == {own.id}
    assert alice_rows[own.id]["knowledge_base_id"] == api.alice_base.id
    assert alice_rows[own.id]["knowledge_base_name"] == "alice-private-kb"
    # 历史遗留的跨用户绑定对 bob 也不能暴露 alice 的知识库
    assert set(bob_rows) == {stolen.id}
    assert bob_rows[stolen.id]["knowledge_base_id"] is None
    assert bob_rows[stolen.id]["knowledge_base_name"] == ""


def _load_backfill_module():
    spec = importlib.util.spec_from_file_location(
        "backfill_knowledge_owner",
        ROOT / "scripts" / "backfill_knowledge_owner.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _add_legacy_base(db, name="legacy-kb", content="legacy content"):
    legacy_base = KnowledgeBase(name=name)
    db.add(legacy_base)
    db.commit()
    legacy_file = KnowledgeFile(
        knowledge_base_id=legacy_base.id,
        name="legacy.txt",
        size=len(content),
        content=content,
    )
    db.add(legacy_file)
    db.commit()
    return legacy_base, legacy_file


def test_backfill_script_assigns_legacy_rows(api):
    backfill_module = _load_backfill_module()
    legacy_base, legacy_file = _add_legacy_base(api.db)

    plan = backfill_module.backfill(api.db, api.alice.id)
    assert plan["applied"] is False
    api.db.expire_all()
    assert api.db.query(KnowledgeBase).filter_by(id=legacy_base.id).first().user_id is None

    plan = backfill_module.backfill(api.db, api.alice.id, apply=True)
    api.db.expire_all()

    assert plan["applied"] is True
    assert api.db.query(KnowledgeBase).filter_by(id=legacy_base.id).first().user_id == api.alice.id
    assert api.db.query(KnowledgeFile).filter_by(id=legacy_file.id).first().user_id == api.alice.id
    assert api.client.get(
        "/api/knowledge",
        params={"knowledge_base_id": legacy_base.id},
        headers=_alice_headers(api),
    ).status_code == 200


def test_backfill_script_reports_conflicts_and_can_rename(api):
    backfill_module = _load_backfill_module()
    owned_base = KnowledgeBase(name="legacy-kb")
    owned_base.user_id = api.alice.id
    api.db.add(owned_base)
    api.db.commit()
    legacy_base, _ = _add_legacy_base(api.db)

    plan = backfill_module.backfill(api.db, api.alice.id, apply=True)
    api.db.expire_all()
    assert plan["applied"] is False
    assert plan["error"]
    assert [(item["id"], item["name"]) for item in plan["conflicts"]] == [(legacy_base.id, "legacy-kb")]
    assert api.db.query(KnowledgeBase).filter_by(id=legacy_base.id).first().user_id is None

    plan = backfill_module.backfill(api.db, api.alice.id, apply=True, rename_conflicts=True)
    api.db.expire_all()

    assert plan["applied"] is True
    renamed = api.db.query(KnowledgeBase).filter_by(id=legacy_base.id).first()
    assert renamed.user_id == api.alice.id
    assert renamed.name == f"legacy-kb-{legacy_base.id}"
