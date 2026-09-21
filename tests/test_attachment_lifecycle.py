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
from model.models import Conversation, Message, User
from router import chat as chat_router
from router import user as user_router
from service import auth_service, chat_service, oss_service, user_service


OSS_CONFIG = {
    "OSS_ACCESS_KEY_ID": "test-id",
    "OSS_ACCESS_KEY_SECRET": "test-secret",
    "OSS_BUCKET": "demo",
    "OSS_ENDPOINT": "https://oss-cn-hangzhou.aliyuncs.com",
}
OSS_HOST = "demo.oss-cn-hangzhou.aliyuncs.com"

# 用例里的对象键写字面量，不从被测实现里取，避免实现改名/改前缀时用例跟着一起「通过」。
KEY_A = "rag-chat/2026/09/21/aaaaaaaaaaaaaaaa.png"
KEY_B = "rag-chat/2026/09/21/bbbbbbbbbbbbbbbb.jpg"
KEY_C = "rag-chat/2026/09/21/cccccccccccccccc.webp"

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
    Base.metadata.create_all(
        bind=engine,
        tables=[User.__table__, Conversation.__table__, Message.__table__],
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
