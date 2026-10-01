"""附件归属收口（issue #233）：登记行是归属的唯一凭据。

修复前的形状：发送成功即把登记行**物理删掉**，删会话时对象键只剩「它出现在我的会话
的消息里」这一条判据。而附件列是 `/api/chat/stream` 的 body 原样落库的（客户端能自由
回带），于是任何登录用户只要在自己的消息里写上别人的对象键，删掉自己的会话就能借服务端
凭据把别人的对象删掉。

修复后的判据：`chat_attachment_uploads.user_id`（**铸键人**）是唯一凭据。

- 消费从「删行」改成盖 `consumed_at`：行留下，归属才留得下；「已消费的行不再是清扫
  候选」由 list_pending_attachment_uploads / claim_attachment_upload 上的
  `consumed_at IS NULL` 过滤承担（可见行为与物理删除时一致）。
- 回收判据从「键出现在我的消息里」改成「这把键是我铸的」：查无登记行、或属主不是我，
  一律拒签，只落一条可对账的告警（不回显给 HTTP 响应）。
- 增长控制：回收成功的键**逐键**释放登记行（`_delete_oss_object` 没抛异常才算成功），
  绝不整批 `IN` 删除。

本文件只测这层判据，清扫任务自身的批处理/并发语义仍由
tests/test_orphan_attachment_cleanup_142.py 覆盖（那个文件在本次改动里预期零改动）。
"""
import asyncio
import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path
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
from model.models import (
    ChatAttachmentUpload,
    ChatTraceSession,
    Conversation,
    Message,
    RevokedToken,
    User,
)
from router import chat as chat_router
from router import user as user_router  # noqa: F401  （与生产同一组路由的导入形态）
from schema.schemas import ChatRequest
from service import auth_service, chat_service, oss_service


ROOT = Path(__file__).resolve().parents[1]

OSS_CONFIG = {
    "OSS_ACCESS_KEY_ID": "test-id",
    "OSS_ACCESS_KEY_SECRET": "test-secret",
    "OSS_BUCKET": "demo",
    "OSS_ENDPOINT": "https://oss-cn-hangzhou.aliyuncs.com",
}
OSS_HOST = "demo.oss-cn-hangzhou.aliyuncs.com"

# 保留窗口的接口契约值写字面量，不从实现里读：窗口被改小/改没时用例要跟着变红。
TTL_SECONDS = 24 * 60 * 60

# 本服务铸过的对象键，形态照抄上传路径实际铸出来的样子
# （rag-chat/<年>/<月>/<日>/<uuid4().hex><扩展名>）。
ATTACH_A = "rag-chat/2026/09/21/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png"
ATTACH_B = "rag-chat/2026/09/21/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.jpg"
KEY_C = "rag-chat/2026/09/21/cccccccccccccccccccccccccccccccc.webp"

# 本服务从没铸过的键（客户端回带的附件列是自由 JSON）。
FOREIGN_KEY = "finance-archive/2026/q3/payroll.sql"
# 形态护栏的近失键：前缀/穿越都很像，但结构上不是本服务铸的。`_delete_oss_object` 只查
# 前缀的实现会在这里删错对象——httpx 会把 `..` 规范化成桶里另一个路径。
NEAR_MISS_KEYS = [
    "rag-chat/2026/09/21/../../finance-archive/2026/q3/payroll.sql",
    "rag-chat/2026/09/21/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png/../../../etc/passwd",
    "rag-chat/2026/9/21/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png",
    "rag-chat/2026/09/21/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA.png",
    "rag-chat/2026/09/21/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.exe",
    "rag-chat/2026/09/21/short.png",
]

PNG_BYTES = b"\x89PNG\r\n\x1a\n-fake-png-payload"


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_text(_type, _compiler, **_kwargs):
    return "TEXT"


class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class _RecordingOssClient:
    """对象存储出口的统一替身：上传（async `put`）与回收（sync `delete`）记在同一个列表里。

    故意不提供别的 HTTP 方法：回收不该走上传路径，上传也不该走删除路径。
    """

    def __init__(self):
        self.requests = []
        self.status_by_key = {}
        self.default_put_status = 200
        self.default_delete_status = 204

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def _status_for(self, url, fallback):
        for key, code in self.status_by_key.items():
            if key in url:
                return code
        return fallback

    async def put(self, url, content=None, headers=None, **_kwargs):
        self.requests.append({"method": "PUT", "url": url, "headers": dict(headers or {})})
        status = self._status_for(url, self.default_put_status)
        return _FakeResponse(status, "InternalError" if status >= 400 else "")

    def delete(self, url, headers=None, **_kwargs):
        self.requests.append({"method": "DELETE", "url": url, "headers": dict(headers or {})})
        status = self._status_for(url, self.default_delete_status)
        return _FakeResponse(status, "AccessDenied" if status >= 400 else "")

    def urls(self, method):
        return [request["url"] for request in self.requests if request["method"] == method]


@pytest.fixture()
def oss_requests(monkeypatch):
    """替换 OSS 的两个 HTTP 出口；同时补齐 OSS 配置。"""
    client = _RecordingOssClient()
    for name, value in OSS_CONFIG.items():
        monkeypatch.setattr(oss_service, name, value)
    monkeypatch.setattr(oss_service.httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(oss_service.httpx, "Client", lambda **_kwargs: client)
    return client


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
            User.__table__, RevokedToken.__table__,
            Conversation.__table__,
            Message.__table__,
            ChatTraceSession.__table__,
            ChatAttachmentUpload.__table__,
        ],
    )
    # 会话配置照抄生产的 database.session.SessionLocal（expire_on_commit 用**默认的 True**）：
    # 写成 False 会让「commit 之后读 ORM 行属性不触发刷新」，把归属判断读到的陈旧状态
    # 悄悄盖掉。
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()

    alice = User(username="alice", password_hash="x")
    bob = User(username="bob", password_hash="x")
    db.add_all([alice, bob])
    db.commit()
    # 主键先取成普通 int：可能发生在一次 `stream_chat` 之后的读，必须拿这个整数值。
    alice_id, bob_id = alice.id, bob.id

    checkpointer_calls = []
    monkeypatch.setattr(chat_service, "delete_thread_checkpoints", checkpointer_calls.append)

    app = FastAPI()
    app.include_router(chat_router.router)
    app.dependency_overrides[db_session.get_db] = lambda: db
    # 按 id 现查，而不是把夹具手里那个实例直接发出去：`stream_chat` 结束时会 close() 掉
    # 夹具借给它的这个会话实例，直接发出去会让后续请求读到已脱管的实例。
    app.dependency_overrides[auth_service.get_current_user] = lambda: db.get(User, alice_id)

    try:
        yield SimpleNamespace(
            client=TestClient(app),
            db=db,
            alice=alice,
            alice_id=alice_id,
            bob=bob,
            bob_id=bob_id,
            checkpointer_calls=checkpointer_calls,
        )
    finally:
        db.close()


def _seed_upload(api, object_key, *, user_id=None, age_seconds=0, consumed=True):
    """直接落一条登记行。

    `consumed=True`（默认）是「这把键已经被它的属主发送过」的形状——也就是它出现在某条
    消息的附件列里时本该有的样子。`consumed=False` 是在途上传（清扫任务的候选）。
    """
    api.db.add(ChatAttachmentUpload(
        object_key=object_key,
        user_id=api.alice_id if user_id is None else user_id,
        created_at=datetime.now() - timedelta(seconds=age_seconds),
        consumed_at=datetime.now() if consumed else None,
    ))
    api.db.commit()


def _attachments_column(*object_keys):
    return [
        {"object_key": key, "url": f"https://{OSS_HOST}/{key}", "name": Path(key).name}
        for key in object_keys
    ]


def _add_conversation(api, cid, messages=(), *, user_id=None):
    """建一个会话，并把附件列**直接写进消息行**。

    不走 `/api/chat/stream`：那条路径上的写入侧形态过滤（`_service_minted_attachments`）
    会把非本服务铸的键丢掉，于是形态护栏的近失键永远到不了回收判据——用例会「因为什么都
    没发生」而变绿。这里要摆的恰恰是「库里已经存着脏键」的存量形状。
    """
    api.db.add(Conversation(id=cid, user_id=api.alice_id if user_id is None else user_id,
                            title=f"会话-{cid}"))
    api.db.commit()
    for index, keys in enumerate(messages):
        api.db.add(Message(
            conversation_id=cid,
            role="user" if index % 2 == 0 else "assistant",
            content="问题",
            attachments=json.dumps(_attachments_column(*keys), ensure_ascii=False),
        ))
    api.db.commit()
    return cid


def _pending_rows(api):
    """清扫任务眼里的候选：登记行还在、且没有被任何消息消费。"""
    from crud import chat as crud_chat

    return crud_chat.list_pending_attachment_uploads(api.db)


def _upload(api, filename="a.png"):
    return api.client.post("/api/chat/attachments", files={"file": (filename, PNG_BYTES, "image/png")})


def _delete_conversation(api, cid):
    return api.client.delete(f"/api/chat/conversations/{cid}")


def _warnings(caplog):
    import logging

    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]


# ---------------------------------------------------------------------------
# N1 / N2：拒签的两条入口——别人的键、以及查无登记行的键
# ---------------------------------------------------------------------------

def test_deleting_a_conversation_never_signs_a_delete_for_another_users_key(
    api, oss_requests, caplog
):
    """别人的键出现在我的消息里，删我的会话也不许为它签 DELETE。

    这条就是 issue #233 的修复面：修复前「键出现在我的会话里」被当成归属凭据，删会话会把
    bob 上传过的对象一并删掉。
    """
    import logging

    _seed_upload(api, ATTACH_B, user_id=api.bob_id)
    _add_conversation(api, "c-mixed", [[ATTACH_B]])

    with caplog.at_level(logging.WARNING, logger="service.chat_service"):
        response = _delete_conversation(api, "c-mixed")

    assert response.status_code == 200
    assert oss_requests.urls("DELETE") == []
    assert any(ATTACH_B in message for message in _warnings(caplog)), "没有留下可按对象对账的告警"
    # 会话本身照删（拒签只影响对象回收，不影响会话删除）。
    assert api.db.query(Conversation).filter_by(id="c-mixed").first() is None
    assert api.checkpointer_calls == ["c-mixed"]
    # 别人的登记行不许被顺手销账：行没了，归属凭据就跟着没了。
    assert api.db.query(ChatAttachmentUpload).filter_by(object_key=ATTACH_B).count() == 1
    assert ATTACH_B not in response.text


def test_a_key_with_no_registry_row_is_never_deleted(api, oss_requests, caplog):
    """deny-by-default：登记表里查无此键，就拒签。

    存量数据（升级前发送成功的键）与客户端自己编的键都会落到这一支。判据缺失时宁可漏删
    ——对象留着的代价是泄漏，删错了没有回收站。
    """
    import logging

    _add_conversation(api, "c-unknown", [[ATTACH_A]])

    with caplog.at_level(logging.WARNING, logger="service.chat_service"):
        response = _delete_conversation(api, "c-unknown")

    assert response.status_code == 200
    assert oss_requests.urls("DELETE") == []
    assert any(ATTACH_A in message for message in _warnings(caplog))
    assert api.db.query(Conversation).filter_by(id="c-unknown").first() is None
    assert ATTACH_A not in response.text


# ---------------------------------------------------------------------------
# N3：回归护栏——属主自己的键照旧回收
# ---------------------------------------------------------------------------

def test_the_owners_own_sent_key_is_still_reclaimed(api, oss_requests):
    """收口不能收成「什么都不删」：属主自己发送过的键，删会话时照旧回收（字节级不变）。"""
    _seed_upload(api, ATTACH_A)
    _add_conversation(api, "c-own", [[ATTACH_A]])

    response = _delete_conversation(api, "c-own")

    assert response.status_code == 200
    assert oss_requests.urls("DELETE") == [f"https://{OSS_HOST}/{ATTACH_A}"]
    assert api.db.query(Conversation).filter_by(id="c-own").first() is None


# ---------------------------------------------------------------------------
# N4：一批混合键，只签请求方自己的
# ---------------------------------------------------------------------------

def test_mixed_batch_only_the_requesters_keys_are_signed(api, oss_requests):
    """一次删会话里同时出现：自己的键、别人的键、没有任何登记行的键、形态不合规的键。

    四个类别各自的处置必须互不干扰，且只有第一个类别产出 DELETE。
    """
    _seed_upload(api, ATTACH_A)
    _seed_upload(api, ATTACH_B, user_id=api.bob_id)
    # 形态不合规的键也给 alice 补上登记行：否则归属判据先一步拒签，形态护栏在这条用例里
    # 根本没被执行到——用例仍绿，但那一段是空的证据。
    for key in (FOREIGN_KEY, *NEAR_MISS_KEYS):
        _seed_upload(api, key)
    _add_conversation(api, "c-batch", [[ATTACH_A, ATTACH_B, KEY_C, FOREIGN_KEY, *NEAR_MISS_KEYS]])

    response = _delete_conversation(api, "c-batch")

    assert response.status_code == 200
    assert oss_requests.urls("DELETE") == [f"https://{OSS_HOST}/{ATTACH_A}"]
    for key in (ATTACH_B, KEY_C, FOREIGN_KEY, *NEAR_MISS_KEYS):
        assert key not in response.text
    assert "finance-archive" not in response.text
    assert api.db.query(Conversation).filter_by(id="c-batch").first() is None


# ---------------------------------------------------------------------------
# N5：别人的在途键不许被我的会话删带走，且仍要能到达清扫任务
# ---------------------------------------------------------------------------

def test_another_users_in_flight_key_survives_my_conversation_delete_and_still_reaches_the_sweeper(
    api, oss_requests
):
    """拒签之后的对象不能就此脱管：它必须原样留在清扫任务的候选集里。

    这是「拒签」与「什么都不管」的分界——拒签只是不替别人销账，不是把对象从所有回收路径
    上摘掉。
    """
    _seed_upload(api, ATTACH_B, age_seconds=TTL_SECONDS + 60, consumed=False, user_id=api.bob_id)
    _add_conversation(api, "c-steal", [[ATTACH_B]])

    assert _delete_conversation(api, "c-steal").status_code == 200
    assert oss_requests.urls("DELETE") == []
    assert [row.object_key for row in _pending_rows(api)] == [ATTACH_B], "bob 的在途登记行被我的删除带走了"

    report = chat_service.reclaim_orphan_chat_attachments(api.db, now=datetime.now())

    assert report["candidates"] == 1
    assert report["reclaimed"] == 1
    assert oss_requests.urls("DELETE") == [f"https://{OSS_HOST}/{ATTACH_B}"]


# ---------------------------------------------------------------------------
# N6 / N7 / N8：消费的可见行为不变（盖时间戳取代物理删行）
# ---------------------------------------------------------------------------

def test_sending_a_message_marks_the_row_consumed_instead_of_deleting_it(
    api, monkeypatch, oss_requests
):
    """发送消费的是「把行盖成已消费」，不是「把行删掉」。

    行是归属凭据，删掉它，删会话时就只剩伪判据可用。同时「已消费的行不再是清扫候选」
    这一条可见行为必须保持不变——否则上传后发送过的对象会被清扫任务二次回收。
    """
    uploaded = _upload(api).json()["object_key"]

    cid = _run_stream_chat(api, monkeypatch, [{"object_key": uploaded, "name": "a.png"}])

    stored = json.loads(api.db.query(Message).filter_by(conversation_id=cid).first().attachments)
    assert [item["object_key"] for item in stored] == [uploaded]

    row = api.db.query(ChatAttachmentUpload).filter_by(object_key=uploaded).one()
    assert row.consumed_at is not None, "发送把登记行消费掉了，但没留下消费时刻"
    assert row.user_id == api.alice_id
    assert _pending_rows(api) == [], "已被消息引用的登记行仍然是清扫候选"


def test_a_consumed_row_is_never_a_sweep_candidate(api, oss_requests):
    """已消费 + 早已超期的行：清扫任务不许碰它引用的对象。"""
    _seed_upload(api, ATTACH_A, age_seconds=TTL_SECONDS + 60, consumed=True)

    report = chat_service.reclaim_orphan_chat_attachments(api.db, now=datetime.now())

    assert report["candidates"] == 0
    assert report["reclaimed"] == 0
    assert oss_requests.urls("DELETE") == []
    assert api.db.query(ChatAttachmentUpload).filter_by(object_key=ATTACH_A).count() == 1


def test_the_sweep_still_reclaims_an_unconsumed_key(api, oss_requests):
    """反过来的一臂：没被消费、又超期的行照旧被回收（收口不能把清扫也收没了）。"""
    _seed_upload(api, ATTACH_A, age_seconds=TTL_SECONDS + 60, consumed=False)

    report = chat_service.reclaim_orphan_chat_attachments(api.db, now=datetime.now())

    assert report["candidates"] == 1
    assert report["reclaimed"] == 1
    assert oss_requests.urls("DELETE") == [f"https://{OSS_HOST}/{ATTACH_A}"]
    assert _pending_rows(api) == []


# ---------------------------------------------------------------------------
# N9：增长控制——回收成功的键才释放登记行，失败的必须留着
# ---------------------------------------------------------------------------

def test_reclaimed_conversation_rows_are_released_after_a_successful_delete(
    api, oss_requests, caplog
):
    """消费改成盖时间戳之后，登记行不再随发送消失，必须在回收成功后逐键释放。

    失败的那把键**必须留着行**：它的对象还在桶里，行是唯一能把它重新对上的凭据。整批
    `IN` 删除会把「哪把键释放了、哪把没有」压成一个数字，失败的键从此既不在清扫候选里、
    也不在回收路径上——泄漏没有任何补偿入口。
    """
    import logging

    _seed_upload(api, ATTACH_A)
    _seed_upload(api, ATTACH_B)
    _add_conversation(api, "c-release", [[ATTACH_A, ATTACH_B]])
    oss_requests.status_by_key[ATTACH_B] = 403

    with caplog.at_level(logging.WARNING, logger="service.chat_service"):
        response = _delete_conversation(api, "c-release")

    assert response.status_code == 200
    # 两把都尝试过（一把成功一把失败），失败不阻断另一把。
    assert sorted(oss_requests.urls("DELETE")) == sorted(
        [f"https://{OSS_HOST}/{ATTACH_A}", f"https://{OSS_HOST}/{ATTACH_B}"]
    )
    assert any(ATTACH_B in message for message in _warnings(caplog))
    assert "403" not in response.text and "AccessDenied" not in response.text

    assert api.db.query(ChatAttachmentUpload).filter_by(object_key=ATTACH_A).count() == 0, (
        "回收成功的键没有释放登记行：这张表只增不减"
    )
    assert api.db.query(ChatAttachmentUpload).filter_by(object_key=ATTACH_B).count() == 1, (
        "回收失败的键被顺手销账了：它的对象还在桶里，行是唯一能对上的凭据"
    )


# ---------------------------------------------------------------------------
# N10：经由真实聊天链路摆出「我的消息引用别人的键」
# ---------------------------------------------------------------------------

def test_a_streaming_message_with_someone_elses_key_cannot_be_used_to_delete_it(
    api, monkeypatch, oss_requests
):
    """走真实 `/api/chat/stream`：把自己的消息摆成「引用别人的键」，随后删会话。

    这条与 N1 的区别在入口：附件列在这里是客户端 body 原样落库的（形态校验放行的是键的
    **形状**，不是归属），所以「我的消息里出现了这把键」是任何登录用户都能制造的形状。
    """
    _seed_upload(api, ATTACH_B, age_seconds=TTL_SECONDS + 60, consumed=False, user_id=api.bob_id)

    cid = _run_stream_chat(api, monkeypatch, [{"object_key": ATTACH_B, "name": "b.jpg"}])

    stored = json.loads(api.db.query(Message).filter_by(conversation_id=cid).first().attachments)
    assert [item["object_key"] for item in stored] == [ATTACH_B]

    response = _delete_conversation(api, cid)

    assert response.status_code == 200
    assert oss_requests.urls("DELETE") == [], "我的消息引用过的别人的键被签了 DELETE"
    assert [row.object_key for row in _pending_rows(api)] == [ATTACH_B], "别人的在途登记行被我的发送或删除改动了"
    assert api.db.query(ChatAttachmentUpload).filter_by(object_key=ATTACH_B).one().consumed_at is None


# ---------------------------------------------------------------------------
# N11：存量键的归属回填
# ---------------------------------------------------------------------------

def _load_backfill_module():
    spec = importlib.util.spec_from_file_location(
        "backfill_attachment_owner",
        ROOT / "scripts" / "backfill_attachment_owner.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backfill_attributes_legacy_keys_to_the_conversation_owner(api, monkeypatch, capsys):
    """存量键（升级前发送成功、登记行被物理删掉的那批）按「会话属主」推定补种登记行。

    三件事：dry-run 只打印、`--apply` 才写；已有行一律不覆盖（无论是否已消费）；重复跑幂等。
    """
    module = _load_backfill_module()
    monkeypatch.setattr(db_session, "SessionLocal", lambda: api.db)

    legacy = "rag-chat/2026/09/21/11111111111111111111111111111111.png"
    rented = "rag-chat/2026/09/21/22222222222222222222222222222222.jpg"
    # bob 上传过、还没发送：这条行携带的是**真实事实**，回填的推定值无权覆盖它。
    _seed_upload(api, rented, user_id=api.bob_id, consumed=False)
    _add_conversation(api, "c-legacy", [[legacy, rented]], user_id=api.alice_id)

    assert module.main([]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["applied"] is False
    assert [item["object_key"] for item in printed["attachments"]] == [legacy]
    assert printed["attachments"][0]["user_id"] == api.alice_id
    assert printed["skipped_existing_rows"] == 1
    assert api.db.query(ChatAttachmentUpload).filter_by(object_key=legacy).count() == 0, (
        "dry-run 写库了"
    )

    assert module.main(["--apply"]) == 0
    capsys.readouterr()
    row = api.db.query(ChatAttachmentUpload).filter_by(object_key=legacy).one()
    assert row.user_id == api.alice_id
    # 必须已消费：这把键本来就在一条消息的附件列里，落成未消费会让清扫任务把一条活消息
    # 正在引用的对象当成孤儿删掉。
    assert row.consumed_at is not None
    assert row.consumed_at == row.created_at

    # 已有行原样保留：属主仍是 bob、仍然是未消费的在途上传。
    kept = api.db.query(ChatAttachmentUpload).filter_by(object_key=rented).one()
    assert kept.user_id == api.bob_id
    assert kept.consumed_at is None

    assert module.main(["--apply"]) == 0
    capsys.readouterr()
    assert api.db.query(ChatAttachmentUpload).filter_by(object_key=legacy).count() == 1, "重复跑不幂等"


# ---------------------------------------------------------------------------
# 启动期补列迁移：存量库没有 consumed_at 这一列
# ---------------------------------------------------------------------------

def test_startup_migration_adds_the_consumed_at_column(monkeypatch, tmp_path):
    """老库升级：`init_db` 的补列迁移必须幂等地补上 `consumed_at`，存量行落成 NULL。

    NULL 恰好等于「还没被消费」——也就是旧代码把登记行物理删掉时，它在清扫任务眼里的
    状态，语义上严格向后兼容。
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE chat_attachment_uploads ("
            "object_key VARCHAR(255) NOT NULL PRIMARY KEY, "
            "user_id INTEGER NOT NULL, "
            "created_at DATETIME NOT NULL)"
        ))
    monkeypatch.setattr(db_session, "engine", engine)

    before = {column["name"] for column in inspect(engine).get_columns("chat_attachment_uploads")}
    assert "consumed_at" not in before

    db_session._ensure_schema_columns()
    after = {column["name"] for column in inspect(engine).get_columns("chat_attachment_uploads")}
    assert "consumed_at" in after

    # 幂等：`init_db` 每次启动都会跑一遍，重复执行既不能报错也不能重复加列。
    db_session._ensure_schema_columns()
    assert {column["name"] for column in inspect(engine).get_columns("chat_attachment_uploads")} == after

    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO chat_attachment_uploads (object_key, user_id, created_at) "
            f"VALUES ('{ATTACH_A}', 1, '2026-01-01 00:00:00')"
        ))
        assert conn.execute(text("SELECT consumed_at FROM chat_attachment_uploads")).scalar() is None


# ---------------------------------------------------------------------------
# 真实 stream_chat 的最小夹具（模型、检索、轨迹短路）
# ---------------------------------------------------------------------------

class _FakeChatTrace:
    def __init__(self, user_id=None):
        self.user_id = user_id
        self.trace_id = "trace-test"

    async def add(self, *_args, **_kwargs):
        pass

    async def attach(self, **_kwargs):
        pass

    async def finish(self, *_args, **_kwargs):
        pass

    def snapshot(self):
        return {"trace_id": self.trace_id, "events": []}


async def _collect_stream(iterator):
    chunks = []
    async for chunk in iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return "".join(chunks)


def _stub_stream_chat_dependencies(api, monkeypatch):
    """把模型、检索、轨迹三块短路掉，只留落库与附件消费这段真实逻辑。"""
    alice_username = api.alice.username

    async def fake_build_effective_question(question, attachments):
        return question, {"status": "skipped"}

    async def fake_recent_memory_text(*_args, **_kwargs):
        return ""

    async def fake_decide_need_rag(*_args, **_kwargs):
        return {"need_rag": False, "route": "direct", "confidence": 1.0, "reason": "test"}

    async def fake_stream_rag_answer(*_args, **_kwargs):
        yield "回答"

    monkeypatch.setattr(
        chat_service,
        "resolve_knowledge_base",
        lambda db, knowledge_base_id, user_id: SimpleNamespace(id=1, name="kb", user_id=user_id),
    )
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: api.db)
    monkeypatch.setattr(
        chat_service,
        "authenticate",
        lambda db, authorization: db.query(User).filter_by(username=alice_username).first(),
    )
    monkeypatch.setattr(chat_service, "TraceRecorder", _FakeChatTrace)
    monkeypatch.setattr(chat_service, "_build_effective_question", fake_build_effective_question)
    monkeypatch.setattr(chat_service, "_build_recent_memory_text", fake_recent_memory_text)
    monkeypatch.setattr(chat_service, "_build_memory_context", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        chat_service, "_build_memory_aware_retrieval_question", lambda question, memory_context: question
    )
    monkeypatch.setattr(chat_service, "decide_need_rag", fake_decide_need_rag)
    monkeypatch.setattr(chat_service, "stream_rag_answer", fake_stream_rag_answer)
    monkeypatch.setattr(chat_service, "_trace_sse_payloads", lambda trace: [])
    monkeypatch.setattr(chat_service, "_build_sources", lambda chunks: [])

    async def _noop_trace(*args, **kwargs):
        return None

    monkeypatch.setattr(chat_service, "_attach_grounding_trace", _noop_trace)
    monkeypatch.setattr(chat_service, "_safe_trace_attach", _noop_trace)
    monkeypatch.setattr(chat_service, "_safe_trace_finish", _noop_trace)
    monkeypatch.setattr(chat_service, "_schedule_memory_summary_update", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "schedule_ragas_evaluation", lambda *args, **kwargs: None)


def _run_stream_chat(api, monkeypatch, attachments) -> str:
    """跑一次真实聊天流（模型、检索、轨迹短路），返回新建会话的 id。"""
    _stub_stream_chat_dependencies(api, monkeypatch)

    # 用取好的整数主键：`stream_chat` 结束时 close() 掉夹具借给它的那个会话实例，之后
    # 现读 api.alice.username 之类会触发脱管实例的属性刷新而抛 DetachedInstanceError。
    alice_id = api.alice_id

    def _conversation_ids():
        return {cid for (cid,) in api.db.query(Conversation.id).filter_by(user_id=alice_id).all()}

    before = _conversation_ids()
    response = asyncio.run(
        chat_service.stream_chat(
            ChatRequest(question="这张图是什么", attachments=attachments),
            authorization="Bearer token",
        )
    )
    asyncio.run(_collect_stream(response.body_iterator))

    created = _conversation_ids() - before
    assert len(created) == 1, f"这次聊天没有新建出唯一一个会话：{created}"
    return created.pop()
