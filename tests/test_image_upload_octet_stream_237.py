"""issue #237 回归：聊天图片上传的判据与知识库侧统一——通用 MIME 按扩展名回退。

修复前 `resolve_image_upload_type` 的回退条件是「归一化后为空」，而 `application/octet-stream`
归一化后是**非空**字符串，于是被判成「明确说了不是图片」，拖拽/命令行上传（浏览器和 CLI 常把
文件标成通用类型）一律 400；知识库路径 `resolve_knowledge_upload_type` 对同样的类型按扩展名
放行。同一张通用类型表，两条判据口径不一致。

这里走**路由层**：真实的 `upload_chat_attachment`（类型判定、5MB 上限、落登记行、铸造对象键
全都在），只把 OSS 的 HTTP 出口换成替身。因此断言同时覆盖三件事——回给前端的 `content_type`、
服务端铸造的 `object_key` 后缀，以及 OSS 实际收到的 URL。只断「200 就算过」的话，判据退化成
「看扩展名」这种回归也照样绿。

头像路径（`user_service.upload_avatar`）**故意不改**：它不传 `allow_filename_fallback`，
octet-stream 仍按原样拒绝——这是边界说明，不是遗漏。
"""

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
from model.models import ChatAttachmentUpload, User
from router import chat as chat_router
from service import auth_service, oss_service
from service.utils_service import CHAT_ATTACHMENT_MAX_BYTES


OSS_CONFIG = {
    "OSS_ACCESS_KEY_ID": "test-id",
    "OSS_ACCESS_KEY_SECRET": "test-secret",
    "OSS_BUCKET": "demo",
    "OSS_ENDPOINT": "https://oss-cn-hangzhou.aliyuncs.com",
}
OSS_HOST = "demo.oss-cn-hangzhou.aliyuncs.com"

# 上传路径不校验魔数，只看声明的类型/扩展名，所以四种后缀共用同一段载荷。
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n-fake-png-payload"

TYPE_ERROR = "仅支持 png、jpg、jpeg、webp 图片"
SIZE_ERROR = "图片不能超过 5MB"


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_text(_type, _compiler, **_kwargs):
    return "TEXT"


class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


@pytest.fixture()
def api(monkeypatch):
    """真实聊天上传路由 + 内存库 + OSS 出口替身。

    只建这条链路真正读写的两张表：`users`（鉴权替身返回的 alice）与
    `chat_attachment_uploads`（上传即登记一条待确认行，issue #142）。
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[User.__table__, ChatAttachmentUpload.__table__],
    )
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()

    alice = User(username="alice", password_hash="x")
    db.add(alice)
    db.commit()

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

    for name, value in OSS_CONFIG.items():
        monkeypatch.setattr(oss_service, name, value)
    monkeypatch.setattr(oss_service.httpx, "AsyncClient", lambda **kwargs: _AsyncRecorder(**kwargs))

    app = FastAPI()
    app.include_router(chat_router.router)
    app.dependency_overrides[db_session.get_db] = lambda: db
    app.dependency_overrides[auth_service.get_current_user] = lambda: alice

    try:
        yield SimpleNamespace(client=TestClient(app), db=db, uploads=uploads)
    finally:
        db.close()


def _upload(api, filename: str, content_type: str, payload: bytes = IMAGE_BYTES):
    return api.client.post(
        "/api/chat/attachments",
        files={"file": (filename, payload, content_type)},
    )


def test_octet_stream_png_falls_back_to_the_filename(api):
    response = _upload(api, "shot.png", "application/octet-stream")

    assert response.status_code == 200
    body = response.json()
    assert body["content_type"] == "image/png"
    assert body["object_key"].endswith(".png")
    # 走到 OSS 的是同一个服务端铸造的键，不是另一条分支上拼出来的。
    assert api.uploads == [f"https://{OSS_HOST}/{body['object_key']}"]


def test_binary_octet_stream_jpeg_falls_back_to_the_filename(api):
    response = _upload(api, "shot.jpg", "binary/octet-stream")

    assert response.status_code == 200
    body = response.json()
    assert body["content_type"] == "image/jpeg"
    assert body["object_key"].endswith(".jpg")
    assert api.uploads == [f"https://{OSS_HOST}/{body['object_key']}"]


def test_octet_stream_with_an_unsupported_extension_is_rejected(api):
    response = _upload(api, "notes.txt", "application/octet-stream")

    assert response.status_code == 400
    assert response.json()["detail"] == TYPE_ERROR
    assert api.uploads == []


def test_octet_stream_with_a_gif_extension_is_rejected(api):
    """gif 是图片但不是受支持的图片：回退到扩展名之后仍要落回白名单判定。"""
    response = _upload(api, "shot.gif", "application/octet-stream")

    assert response.status_code == 400
    assert response.json()["detail"] == TYPE_ERROR
    assert api.uploads == []


def test_missing_content_type_still_falls_back_to_the_filename(api):
    """空类型是通用类型表里的第一个成员，行为与修复前一致。"""
    response = _upload(api, "shot.png", "")

    assert response.status_code == 200
    body = response.json()
    assert body["content_type"] == "image/png"
    assert body["object_key"].endswith(".png")


def test_explicit_image_content_type_is_untouched(api):
    """阳性对照：本来就走得通的那条路，不能被这次的放宽改坏。"""
    response = _upload(api, "shot.png", "image/png")

    assert response.status_code == 200
    body = response.json()
    assert body["content_type"] == "image/png"
    assert body["object_key"].endswith(".png")


def test_octet_stream_over_the_size_limit_hits_the_size_check(api):
    """判据放行之后，大小检查必须仍然挡在前面。

    修复前这条会以「仅支持 png…」而非「图片不能超过 5MB」失败——类型判定在大小之前，
    两种失败文案正好把两个检查点区分开。
    """
    oversized = IMAGE_BYTES + b"a" * CHAT_ATTACHMENT_MAX_BYTES

    response = _upload(api, "shot.png", "application/octet-stream", oversized)

    assert response.status_code == 400
    assert response.json()["detail"] == SIZE_ERROR
    assert api.uploads == []
