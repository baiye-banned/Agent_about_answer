"""issue #11 回归：知识库上传必须先校验类型与大小，再决定是否把文件读进内存。

覆盖点：
- 超出上限的文件返回 400，且不落库、不写向量；
- 白名单外的扩展名 / MIME 返回 400，不再兜底当纯文本解码；
- 合法文件仍能正常抽取、落库、写向量，护栏没有误伤正常链路。
"""
import asyncio
import io
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import session as db_session
from database.session import Base
from model.models import KnowledgeBase, KnowledgeFile, User
from router import auth as auth_router
from router import knowledge as knowledge_router
from service import auth_service, knowledge_service, utils_service


USERNAME = "alice"
PASSWORD = "alice-pass"
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


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
        tables=[User.__table__, KnowledgeBase.__table__, KnowledgeFile.__table__],
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    db = TestingSession()

    alice = User(username=USERNAME, password_hash=auth_service.pwd_context.hash(PASSWORD))
    db.add(alice)
    db.commit()
    base = KnowledgeBase(name="alice-kb", user_id=alice.id)
    db.add(base)
    db.commit()

    app = FastAPI()
    app.include_router(auth_router.router)
    app.include_router(knowledge_router.router)
    app.dependency_overrides[db_session.get_db] = lambda: db

    # 向量库不参与校验，用例里记录调用即可，避免连真实 Milvus。
    indexed: list[list[dict]] = []
    monkeypatch.setattr(knowledge_service, "add_chunks", lambda chunks, *args, **kwargs: indexed.append(chunks))
    monkeypatch.setattr(knowledge_service, "delete_file_chunks", lambda *args, **kwargs: None)

    client = TestClient(app)
    login = client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    try:
        yield SimpleNamespace(client=client, db=db, base=base, indexed=indexed, headers=headers)
    finally:
        db.close()


def _upload(api, filename, content, content_type):
    return api.client.post(
        "/api/knowledge/upload",
        files={"file": (filename, content, content_type)},
        data={"knowledge_base_id": str(api.base.id)},
        headers=api.headers,
    )


def _stored_files(api):
    api.db.expire_all()
    return api.db.query(KnowledgeFile).all()


def _build_docx(text: str) -> bytes:
    from docx import Document

    buffer = io.BytesIO()
    document = Document()
    document.add_paragraph(text)
    document.save(buffer)
    return buffer.getvalue()


class _FakeUpload:
    """模拟 UploadFile 的分块读取，用于验证读取过程不会整体吞下超限文件。"""

    def __init__(self, chunk: bytes, total_chunks: int):
        self.chunk = chunk
        self.remaining = total_chunks
        self.read_sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if self.remaining <= 0:
            return b""
        self.remaining -= 1
        return self.chunk


def test_oversized_upload_is_rejected_and_not_persisted(api):
    payload = b"a" * (utils_service.KNOWLEDGE_UPLOAD_MAX_BYTES + 1)

    response = _upload(api, "big.txt", payload, "text/plain")

    assert response.status_code == 400
    assert "超过" in response.json()["detail"]
    assert _stored_files(api) == []
    assert api.indexed == []


def test_oversized_upload_is_rejected_in_chunked_read(api, monkeypatch):
    # 放大允许的 multipart 开销，等于关掉 content-length 预检，只留读取时的二次核验。
    monkeypatch.setattr(knowledge_service, "MULTIPART_OVERHEAD_ALLOWANCE_BYTES", 1 << 30)
    payload = b"a" * (utils_service.KNOWLEDGE_UPLOAD_MAX_BYTES + 1)

    response = _upload(api, "big.txt", payload, "text/plain")

    assert response.status_code == 400
    assert "超过" in response.json()["detail"]
    assert _stored_files(api) == []
    assert api.indexed == []


def test_content_length_precheck_rejects_before_reading_body(api, monkeypatch):
    async def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("超限请求应在 content-length 预检处被拒绝，不应再进入读取流程")

    monkeypatch.setattr(knowledge_service, "read_upload_within_limit", _fail_if_called)
    payload = b"a" * (utils_service.KNOWLEDGE_UPLOAD_MAX_BYTES + 4096)

    response = _upload(api, "big.txt", payload, "text/plain")

    assert response.status_code == 400
    assert _stored_files(api) == []


def test_upload_exactly_at_limit_is_accepted(api):
    # 边界：恰好达到上限的文件必须放行，预检的 multipart 开销余量不能反过来误伤合法上传。
    payload = b"a" * utils_service.KNOWLEDGE_UPLOAD_MAX_BYTES

    response = _upload(api, "exact.txt", payload, "text/plain")

    assert response.status_code == 200
    assert response.json()["size"] == utils_service.KNOWLEDGE_UPLOAD_MAX_BYTES
    assert len(_stored_files(api)) == 1


def test_unsupported_extension_is_rejected(api):
    response = _upload(api, "run.exe", b"MZ" + b"\x00" * 1024, "application/x-msdownload")

    assert response.status_code == 400
    assert response.json()["detail"] == utils_service.KNOWLEDGE_UPLOAD_TYPE_ERROR_MESSAGE
    assert _stored_files(api) == []
    assert api.indexed == []


def test_upload_without_filename_is_rejected(api):
    # 没有 filename 的 multipart 部分不会被 FastAPI 当作文件，请求在框架层（422）就被拒绝；
    # 无论状态码来自哪一层，都不能落库。
    response = api.client.post(
        "/api/knowledge/upload",
        files={"file": ("", b"payload", "text/plain")},
        data={"knowledge_base_id": str(api.base.id)},
        headers=api.headers,
    )

    assert response.status_code == 422
    assert _stored_files(api) == []


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [
        ("notes.txt", "application/x-msdownload"),
        ("notes.md", "application/pdf"),
        ("report.pdf", "image/png"),
        ("report.docx", "text/plain"),
    ],
)
def test_declared_content_type_must_match_extension(api, filename, content_type):
    response = _upload(api, filename, b"payload", content_type)

    assert response.status_code == 400
    assert _stored_files(api) == []


def test_supported_text_file_is_extracted_indexed_and_persisted(api):
    payload = "# 迟到处理\n\n迟到 30 分钟以内记口头提醒。".encode()

    response = _upload(api, "rule.md", payload, "text/plain")

    assert response.status_code == 200
    assert response.json()["name"] == "rule.md"
    assert response.json()["size"] == len(payload)
    stored = _stored_files(api)
    assert len(stored) == 1
    assert "口头提醒" in stored[0].content
    assert api.indexed and api.indexed[0]


def test_generic_content_type_is_accepted_by_extension(api):
    response = _upload(api, "notes.txt", b"hello", "application/octet-stream")

    assert response.status_code == 200
    assert len(_stored_files(api)) == 1


def test_supported_docx_is_extracted(api):
    response = _upload(api, "report.docx", _build_docx("季度目标：完成上传校验"), DOCX_CONTENT_TYPE)

    assert response.status_code == 200
    assert "季度目标" in _stored_files(api)[0].content


def test_read_upload_within_limit_returns_content_under_limit():
    fake = _FakeUpload(b"abc", 2)

    assert asyncio.run(knowledge_service.read_upload_within_limit(fake, 10)) == b"abcabc"
    assert set(fake.read_sizes) == {knowledge_service.UPLOAD_READ_CHUNK_BYTES}


def test_read_upload_within_limit_aborts_right_after_crossing_limit():
    chunk = b"x" * knowledge_service.UPLOAD_READ_CHUNK_BYTES
    fake = _FakeUpload(chunk, 8)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(knowledge_service.read_upload_within_limit(fake, 3 * knowledge_service.UPLOAD_READ_CHUNK_BYTES))

    assert excinfo.value.status_code == 400
    # 读到第 4 块（累计 4MB > 3MB）立即中断，剩余 4 块没有被读取。
    assert len(fake.read_sizes) == 4
    assert fake.remaining == 4


def test_upload_body_exceeds_limit_tolerates_multipart_overhead():
    max_bytes = 1024
    allowance = knowledge_service.MULTIPART_OVERHEAD_ALLOWANCE_BYTES

    assert knowledge_service.upload_body_exceeds_limit(None, max_bytes) is False
    assert knowledge_service.upload_body_exceeds_limit("", max_bytes) is False
    assert knowledge_service.upload_body_exceeds_limit("not-a-number", max_bytes) is False
    assert knowledge_service.upload_body_exceeds_limit(str(max_bytes + allowance), max_bytes) is False
    assert knowledge_service.upload_body_exceeds_limit(str(max_bytes + allowance + 1), max_bytes) is True
