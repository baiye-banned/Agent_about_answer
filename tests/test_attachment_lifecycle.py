"""issue #128 回归：聊天附件与头像这两类「派生文件」的回收路径。

修复前两条上传路径都没有对应的删除路径：

- 删除会话只删 `messages` 行（附件对象键随之从库里消失），OSS 上按公开读写入的对象
  继续可匿名下载；
- 换头像只写新文件并更新 `users.avatar`，被替换下来的旧文件继续能被静态挂载访问。

这里对两条路径各锁行为，断言的是**外部可见结果**而不是内部函数：

- OSS 面替换掉 `httpx.Client`（真实请求出口）记录发往对象存储的请求，因此断言覆盖
  请求方法、URL 与签名头——替身若把被测函数整个换掉，就断不出「请求根本没发出去」；
- 头像面用临时目录里的真实文件，断言旧文件在磁盘上确实消失。

另有两条「护栏」用例（首传无旧文件、同秒同名重传）在修复前也通过：它们锁的是新删除
路径不得误删，用来挡住「不分青红皂白 unlink」的粗暴实现。
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime
from email.utils import formatdate
from pathlib import Path
from types import SimpleNamespace

import httpx
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
from model.models import ChatAttachmentUpload, ChatTraceSession, Conversation, Message, User
from router import chat as chat_router
from router import user as user_router
from schema.schemas import ChatRequest
from service import auth_service, chat_service, oss_service, user_service


OSS_CONFIG = {
    "OSS_ACCESS_KEY_ID": "test-id",
    "OSS_ACCESS_KEY_SECRET": "test-secret",
    "OSS_BUCKET": "demo",
    "OSS_ENDPOINT": "https://oss-cn-hangzhou.aliyuncs.com",
}
OSS_HOST = "demo.oss-cn-hangzhou.aliyuncs.com"

# 用例里的对象键写字面量，不从被测实现里取，避免实现改名/改前缀时用例跟着一起「通过」。
# 形态照抄上传路径实际铸出来的样子：rag-chat/<年>/<月>/<日>/<uuid4().hex><扩展名>。
KEY_A = "rag-chat/2026/09/21/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png"
KEY_B = "rag-chat/2026/09/21/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.jpg"
KEY_C = "rag-chat/2026/09/21/cccccccccccccccccccccccccccccccc.webp"

# 客户端的附件列表是回带上传接口返回值的自由 JSON（schema/schemas.py 的 list[dict] 不校验），
# 所以库里可能出现本服务从没铸过的键；它一旦进了删除链路就会被签成服务端 DeleteObject。
FOREIGN_KEY = "finance-archive/2026/q3/payroll.sql"
# 形似而实非的键：前缀对得上但结构不对。第二条尤其重要——`quote(key, safe="/")` 会把它
# 原样拼进 URL，而 httpx 会把 `..` 规范化掉，只查前缀的实现会在这里删到桶里别的对象。
NEAR_MISS_KEYS = [
    "rag-chat/../../finance-archive/2026/q3/payroll.sql",
    "rag-chat/2026/09/21/../../finance-archive/2026/q3/payroll.sql",
    "rag-chat/2026/09/21/",
    "rag-chat/2026/09/21/not-a-uuid.png",
    "rag-chat/2026/09/21/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.sh",
    "other-bucket/2026/09/21/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png",
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
    """`httpx.Client` 的最小替身：记录请求，并按对象键返回可配置的状态码。

    只实现被测链路真正用到的方法，故意不提供 `put`：附件回收不该走上传路径。
    """

    def __init__(self):
        self.requests = []
        self.status_by_key = {}
        self.default_status = 204

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def delete(self, url, headers=None, **_kwargs):
        self.requests.append({"method": "DELETE", "url": url, "headers": dict(headers or {})})
        status = self.default_status
        for key, code in self.status_by_key.items():
            if key in url:
                status = code
        return _FakeResponse(status, "AccessDenied" if status >= 400 else "")

    def deleted_urls(self):
        return [request["url"] for request in self.requests if request["method"] == "DELETE"]


class _SteppingClock:
    """替身时钟：每次 `now()` 前进一秒，让连续上传拿到可预期的文件名。

    真实时间戳是秒级的，连续两次上传很容易落在同一秒——那样文件名相同、旧文件被覆盖
    而不是替换，用例就断不出「旧文件被删」。这里把时间轴拉直。
    """

    def __init__(self, start=1_700_000_000):
        self._timestamp = start

    def now(self):
        self._timestamp += 1
        return datetime.fromtimestamp(self._timestamp)


class _FrozenClock:
    """替身时钟：`now()` 恒定，用来制造「同一秒内重复上传同扩展名」的覆盖场景。"""

    def __init__(self, timestamp=1_700_000_000):
        self._timestamp = timestamp

    def now(self):
        return datetime.fromtimestamp(self._timestamp)


@pytest.fixture()
def oss_requests(monkeypatch):
    """替换 OSS 的 HTTP 出口，记录回收请求；同时补齐 OSS 配置。"""
    client = _RecordingOssClient()
    for name, value in OSS_CONFIG.items():
        monkeypatch.setattr(oss_service, name, value)
    monkeypatch.setattr(oss_service.httpx, "Client", lambda **_kwargs: client)
    return client


@pytest.fixture()
def api(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # 会话删除链路读写的表都要建出来。chat_trace_sessions 是删会话时清学习轨迹用的
    # （crud/chat.py 的 delete_conversation），少了它，用例只会在这条链路合入后的
    # CI（跑的是与 develop 合并后的 merge ref）上红，本地分支上反倒看不出问题。
    # chat_attachment_uploads 同理，是 issue #142 之后上传与发送链路都要写的登记表。
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            Conversation.__table__,
            Message.__table__,
            ChatTraceSession.__table__,
            ChatAttachmentUpload.__table__,
        ],
    )
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()

    alice = User(username="alice", password_hash="x")
    db.add(alice)
    db.commit()

    # 头像目录换成临时目录：断言的是磁盘上的真实文件，不碰仓库里的 backend/uploads/。
    avatar_dir = tmp_path / "avatars"
    avatar_dir.mkdir()
    monkeypatch.setattr(user_service, "AVATAR_DIR", avatar_dir)
    monkeypatch.setattr(user_service, "datetime", _SteppingClock())

    # 会话删除还会清理 checkpointer 的 sqlite 文件；替换为空操作，避免用例写真实文件，
    # 同时保留「主流程仍然调用它」的断言能力。
    checkpointer_calls = []
    monkeypatch.setattr(chat_service, "delete_thread_checkpoints", checkpointer_calls.append)

    app = FastAPI()
    app.include_router(chat_router.router)
    app.include_router(user_router.router)
    app.dependency_overrides[db_session.get_db] = lambda: db
    app.dependency_overrides[auth_service.get_current_user] = lambda: alice

    try:
        yield SimpleNamespace(
            client=TestClient(app),
            db=db,
            alice=alice,
            avatar_dir=avatar_dir,
            checkpointer_calls=checkpointer_calls,
        )
    finally:
        db.close()


def _attachments_column(*object_keys) -> str:
    """按上传接口回给前端的形状构造 `messages.attachments` 列值。"""
    return json.dumps(
        [
            {"object_key": key, "url": f"https://{OSS_HOST}/{key}", "name": Path(key).name}
            for key in object_keys
        ],
        ensure_ascii=False,
    )


def _add_conversation(api, cid, attachments_columns):
    """建会话；`attachments_columns` 是每条消息的 `attachments` 列原文，原样落库。"""
    api.db.add(Conversation(id=cid, user_id=api.alice.id, title=f"会话-{cid}"))
    api.db.commit()
    for index, column in enumerate(attachments_columns):
        api.db.add(Message(
            conversation_id=cid,
            role="user" if index % 2 == 0 else "assistant",
            content=f"内容-{index}",
            sources="[]",
            attachments=column,
        ))
    api.db.commit()


def _upload_avatar(api, payload: bytes, content_type="image/png", filename="avatar.png"):
    return api.client.post(
        "/api/user/avatar",
        files={"file": (filename, payload, content_type)},
    )


def _avatar_file(api, avatar_path: str) -> Path:
    return api.avatar_dir / Path(avatar_path).name


# ---------------------------------------------------------------------------
# (a) 删除会话回收聊天附件对象
# ---------------------------------------------------------------------------

def test_deleting_conversation_deletes_referenced_attachment_objects(api, oss_requests):
    _add_conversation(api, "c-del", [
        _attachments_column(KEY_A),
        _attachments_column(KEY_B, KEY_C),   # 多图消息
        "",                                   # 无附件的消息
        _attachments_column(KEY_A),           # 同一对象被两条消息引用：只该删一次
        "not-json",                           # 历史脏数据：列里不是 JSON
        json.dumps([{"name": "无对象键.png"}]),  # 上传中断留下的缺键记录
    ])

    response = api.client.delete("/api/chat/conversations/c-del")

    assert response.status_code == 200
    assert sorted(oss_requests.deleted_urls()) == sorted([
        f"https://{OSS_HOST}/{KEY_A}",
        f"https://{OSS_HOST}/{KEY_B}",
        f"https://{OSS_HOST}/{KEY_C}",
    ])
    assert api.checkpointer_calls == ["c-del"]
    assert api.db.query(Conversation).filter_by(id="c-del").first() is None
    assert api.db.query(Message).filter_by(conversation_id="c-del").first() is None


def test_attachment_delete_request_is_signed_for_oss_delete(api, monkeypatch):
    """回收请求本身要是 OSS 认得的 DeleteObject，而不是「随便发了个请求」。

    这条用真实 `httpx.Client` + 模拟传输层：请求对象、URL 编码、头部构造都走真库代码，
    只有网络出口被换掉。手写替身容易把「客户端怎么构造请求」这段一起替掉，掩盖真实差异。
    """
    seen = []
    transport = httpx.MockTransport(
        lambda request: (seen.append(request), httpx.Response(204))[1]
    )
    real_client_cls = httpx.Client  # factory 内部若直接取 httpx.Client 会取到被替换后的自己

    def factory(**kwargs):
        return real_client_cls(transport=transport, **kwargs)

    for name, value in OSS_CONFIG.items():
        monkeypatch.setattr(oss_service, name, value)
    monkeypatch.setattr(oss_service.httpx, "Client", factory)

    _add_conversation(api, "c-sign", [_attachments_column(KEY_A)])

    assert api.client.delete("/api/chat/conversations/c-sign").status_code == 200

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "DELETE"
    assert str(request.url) == f"https://{OSS_HOST}/{KEY_A}"
    assert request.headers["Host"] == OSS_HOST
    assert request.headers["Authorization"].startswith("OSS test-id:")

    # 签名必须由真实 string-to-sign 推出（DELETE + 空 Content-MD5/Content-Type + 资源路径）。
    date = request.headers["Date"]
    expected = base64.b64encode(
        hmac.new(
            b"test-secret",
            f"DELETE\n\n\n{date}\n/demo/{KEY_A}".encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("utf-8")
    assert request.headers["Authorization"] == f"OSS test-id:{expected}"
    assert date == formatdate(usegmt=True)
    # 回收不该带上传时那条公开读 ACL 头：对象都要删了，改 ACL 没有意义且会改变签名内容。
    assert "x-oss-object-acl" not in request.headers
    # 也不能带 content-type：签名串的 Content-Type 槽位留空（上面的 string-to-sign 是
    # `DELETE\n\n\n{date}\n{resource}`），真发出去时若被补上 content-type，签名就对不上了。
    assert "content-type" not in request.headers


def test_failed_object_delete_keeps_the_conversation_delete_working(api, oss_requests, caplog):
    """单个对象删不掉：会话照删，失败落服务端日志，且不回显给用户。"""
    _add_conversation(api, "c-partial", [_attachments_column(KEY_A, KEY_B, KEY_C)])
    oss_requests.status_by_key[KEY_B] = 403

    with caplog.at_level(logging.WARNING, logger="service.chat_service"):
        response = api.client.delete("/api/chat/conversations/c-partial")

    assert response.status_code == 200
    # 一个对象失败不影响其余对象的回收。
    assert sorted(oss_requests.deleted_urls()) == sorted([
        f"https://{OSS_HOST}/{KEY_A}",
        f"https://{OSS_HOST}/{KEY_B}",
        f"https://{OSS_HOST}/{KEY_C}",
    ])
    assert api.db.query(Conversation).filter_by(id="c-partial").first() is None
    assert api.db.query(Message).filter_by(conversation_id="c-partial").first() is None

    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert any(KEY_B in record.getMessage() for record in warnings)
    # 上游错误文本只进日志，不进响应体。
    assert "403" not in response.text and "AccessDenied" not in response.text


def test_attachment_objects_are_deleted_only_after_the_rows_are_gone(api, oss_requests, monkeypatch):
    """顺序护栏：先落库、后回收。

    删行失败时一个对象都不许删——对象存储没有回收站，先删对象再删行，一旦删行失败就会
    留下一个仍然存在的会话、里面图片全部失效；反过来最坏只是留个可重跑的孤儿对象。
    """
    _add_conversation(api, "c-order", [_attachments_column(KEY_A)])

    def _failing_commit():
        raise RuntimeError("commit failed")

    monkeypatch.setattr(api.db, "commit", _failing_commit)

    with pytest.raises(RuntimeError):
        api.client.delete("/api/chat/conversations/c-order")

    assert oss_requests.requests == []


def test_deleting_missing_conversation_does_not_touch_object_storage(api, oss_requests):
    response = api.client.delete("/api/chat/conversations/c-absent")

    assert response.status_code == 404
    assert oss_requests.requests == []


# ---------------------------------------------------------------------------
# (b) 换头像回收旧文件
# ---------------------------------------------------------------------------

def test_replacing_avatar_removes_the_replaced_file(api):
    first = _upload_avatar(api, b"first-avatar")
    assert first.status_code == 200
    first_path = first.json()["avatar"]
    assert _avatar_file(api, first_path).read_bytes() == b"first-avatar"

    second = _upload_avatar(api, b"second-avatar")
    assert second.status_code == 200
    second_path = second.json()["avatar"]
    assert second_path != first_path

    assert _avatar_file(api, second_path).read_bytes() == b"second-avatar"
    assert not _avatar_file(api, first_path).exists()
    assert api.alice.avatar == second_path


def test_repeated_avatar_replacements_leave_only_the_newest_file(api):
    paths = []
    for index in range(3):
        response = _upload_avatar(api, f"avatar-{index}".encode())
        assert response.status_code == 200
        paths.append(response.json()["avatar"])

    assert len(set(paths)) == 3
    for stale in paths[:-1]:
        assert not _avatar_file(api, stale).exists()
    assert _avatar_file(api, paths[-1]).read_bytes() == b"avatar-2"
    assert [path.name for path in sorted(api.avatar_dir.iterdir())] == [Path(paths[-1]).name]


def test_first_avatar_upload_skips_removal_without_previous_file(api):
    """护栏：没有旧头像时（首次上传）不得因为「找不到旧文件」而失败。"""
    assert api.alice.avatar in (None, "")

    response = _upload_avatar(api, b"only-avatar")

    assert response.status_code == 200
    assert _avatar_file(api, response.json()["avatar"]).read_bytes() == b"only-avatar"


def test_avatar_upload_same_second_does_not_delete_the_new_file(api, monkeypatch):
    """护栏：同秒 + 同扩展名会落到同一个文件名，此时旧路径就是新文件，不得删除。"""
    monkeypatch.setattr(user_service, "datetime", _FrozenClock())

    first = _upload_avatar(api, b"same-second-first")
    second = _upload_avatar(api, b"same-second-second")

    assert first.json()["avatar"] == second.json()["avatar"]
    assert _avatar_file(api, second.json()["avatar"]).read_bytes() == b"same-second-second"


def test_replaced_avatar_is_removed_only_after_the_new_path_is_stored(api, monkeypatch):
    """顺序护栏：先落库、后删文件。

    avatar 列没更新成功时旧文件必须还在——否则头像列会指向一个已被删掉的文件，
    用户头像直接 404，而旧文件已经找不回来了。
    """
    first = _upload_avatar(api, b"first-avatar")
    first_path = first.json()["avatar"]

    def _failing_commit():
        raise RuntimeError("commit failed")

    monkeypatch.setattr(api.db, "commit", _failing_commit)

    with pytest.raises(RuntimeError):
        _upload_avatar(api, b"second-avatar")

    assert _avatar_file(api, first_path).exists()


def test_avatar_removal_stays_inside_the_avatar_directory(api, tmp_path):
    """护栏：头像路径是库里的列值，指向目录外时不得跟着删。"""
    outsider = tmp_path / "outsider.png"
    outsider.write_bytes(b"untouched")
    api.alice.avatar = "/uploads/avatars/../outsider.png"
    api.db.commit()

    response = _upload_avatar(api, b"new-avatar")

    assert response.status_code == 200
    assert outsider.read_bytes() == b"untouched"


# ---------------------------------------------------------------------------
# (c) 对象键命名空间护栏：只为本服务铸造的键签发 DELETE
# ---------------------------------------------------------------------------

def test_foreign_object_key_is_never_signed_for_delete(api, oss_requests, caplog):
    """附件列里的键由客户端回带，不得拿服务端凭据为陌生键签 DeleteObject。

    `schema/schemas.py` 的 `attachments: list[dict]` 不校验、`stream_chat` 原样落库，所以
    桶里除聊天附件以外的东西都可能出现在这个键上。这里直接把脏键写进库（最坏前提，绕开
    写入侧那道过滤），删会话时：本服务的键照删，其余一个请求都不许发出去。

    `NEAR_MISS_KEYS` 里的穿越键是这条护栏的重点：键会被 `quote(key, safe="/")` 拼进 URL，
    httpx 会把 `..` 规范化掉，于是 `rag-chat/../../finance-archive/x` 会以 `/finance-archive/x`
    发出去——只查 `rag-chat/` 前缀的实现在这里就会删到桶里别的对象。
    """
    _add_conversation(api, "c-foreign", [
        _attachments_column(FOREIGN_KEY),
        _attachments_column(*NEAR_MISS_KEYS),
        _attachments_column(KEY_A),
    ])

    with caplog.at_level(logging.WARNING, logger="service.chat_service"):
        response = api.client.delete("/api/chat/conversations/c-foreign")

    assert response.status_code == 200
    assert oss_requests.deleted_urls() == [f"https://{OSS_HOST}/{KEY_A}"]

    # 被跳过的每个键都要留下可按对象对账的 warning（会话删了，日志是唯一线索）。
    warnings = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    for key in [FOREIGN_KEY, *NEAR_MISS_KEYS]:
        assert any(key in message for message in warnings), f"没有为 {key!r} 留下告警"

    # 跳过的是对象，不是这次删除：行照删，被拒的键只进日志、不回显给用户。
    assert api.db.query(Conversation).filter_by(id="c-foreign").first() is None
    assert api.db.query(Message).filter_by(conversation_id="c-foreign").first() is None
    assert "finance-archive" not in response.text


def test_guard_accepts_the_key_the_upload_path_actually_mints(api, monkeypatch, oss_requests):
    """两端对齐：上传接口真铸出来的键必须过得了删除侧的护栏。

    上面几条用的键是字面量（写得跟铸造结果一样），但字面量锁不住「铸造形态改了、护栏没跟」
    这种漂移——那会让回收静默停止，而用例全绿。这条从真实上传接口取一个键（只把 OSS 出口
    换成替身），再拿它走一遍删除链路，漂移就会在这里变红。
    """
    uploads = []

    class _AsyncRecorder:
        """`_put_oss_object` 的出口替身：只记请求，不联网。"""

        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def put(self, url, content=None, headers=None, **_kwargs):
            uploads.append(url)
            return _FakeResponse(200)

    monkeypatch.setattr(oss_service.httpx, "AsyncClient", lambda **kwargs: _AsyncRecorder(**kwargs))

    response = api.client.post("/api/chat/attachments", files={"file": ("a.png", PNG_BYTES, "image/png")})

    assert response.status_code == 200
    minted_key = response.json()["object_key"]
    # 确实走的是真实铸造代码，而不是被替身整个换掉的一段。
    assert uploads == [f"https://{OSS_HOST}/{minted_key}"]

    _add_conversation(api, "c-minted", [_attachments_column(minted_key)])
    assert api.client.delete("/api/chat/conversations/c-minted").status_code == 200
    assert oss_requests.deleted_urls() == [f"https://{OSS_HOST}/{minted_key}"]


def test_foreign_object_key_is_not_stored_by_the_chat_route(api, monkeypatch, oss_requests):
    """端到端：客户端回带的外来键既不落库，也不会变成服务端签发的 DELETE。

    走真实 `stream_chat`（模型与检索短路），把评审 PoC 的链路固化成回归：污点从
    `/api/chat/stream` 的 body 进来，看它落在库里是什么、删会话时又发了什么请求。
    与上一条互补——上一条锁删除侧的收口，这一条锁写入侧的收口，去掉任意一道，
    对应用例变红。
    """
    cid = _run_stream_chat(api, monkeypatch, [
        {"object_key": FOREIGN_KEY, "name": "payroll.sql"},
        {"object_key": NEAR_MISS_KEYS[0], "name": "escape.png"},
        {"object_key": KEY_A, "name": "a.png"},
    ])

    stored = json.loads(api.db.query(Message).filter_by(conversation_id=cid).first().attachments)
    assert [item["object_key"] for item in stored] == [KEY_A]

    assert api.client.delete(f"/api/chat/conversations/{cid}").status_code == 200
    assert oss_requests.deleted_urls() == [f"https://{OSS_HOST}/{KEY_A}"]


class _FakeChatTrace:
    """聊天链路只用到 trace 的这几个方法，用例里不落库。"""

    def __init__(self, user_id=None):
        self.user_id = user_id
        self.trace_id = "trace-test"

    def add(self, *_args, **_kwargs):
        pass

    def attach(self, **_kwargs):
        pass

    def finish(self, *_args, **_kwargs):
        pass

    def snapshot(self):
        return {"trace_id": self.trace_id, "events": []}


async def _collect_stream(iterator):
    chunks = []
    async for chunk in iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return "".join(chunks)


def _run_stream_chat(api, monkeypatch, attachments) -> str:
    """跑一次真实聊天流（模型、检索、轨迹短路），返回新建会话的 id。"""

    async def fake_build_effective_question(question, attachments):
        return question, {"status": "skipped"}

    async def fake_recent_memory_text(*_args, **_kwargs):
        return ""

    async def fake_decide_need_rag(*_args, **_kwargs):
        return {"need_rag": False, "route": "direct", "confidence": 1.0, "reason": "test"}

    async def fake_stream_rag_answer(*_args, **_kwargs):
        yield "回答"

    # 这个夹具只建 User/Conversation/Message 三张表，知识库解析短路掉（本用例与检索无关）。
    monkeypatch.setattr(
        chat_service,
        "resolve_knowledge_base",
        lambda db, knowledge_base_id, user_id: SimpleNamespace(id=1, name="kb", user_id=user_id),
    )
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: api.db)
    monkeypatch.setattr(chat_service, "decode_token", lambda authorization: api.alice.username)
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
    monkeypatch.setattr(chat_service, "_attach_grounding_trace", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "_safe_trace_attach", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "_safe_trace_finish", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "_schedule_memory_summary_update", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "schedule_ragas_evaluation", lambda *args, **kwargs: None)

    def _conversation_ids():
        return {cid for (cid,) in api.db.query(Conversation.id).filter_by(user_id=api.alice.id).all()}

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
