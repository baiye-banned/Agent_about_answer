import asyncio
import logging

import pytest
from fastapi import HTTPException

from rag import milvus_client
from service import knowledge_service


class _FileEntry:
    def __init__(self, file_id):
        self.id = file_id


class _UploadFile:
    def __init__(self, filename, content, content_type="text/plain"):
        self.filename = filename
        self.content_type = content_type
        self._content = content

    async def read(self, size=-1):
        # 与 UploadFile 一致：按 size 切分，读完返回空串，否则调用方会一直读到内容。
        if size is None or size < 0:
            chunk, self._content = self._content, b""
            return chunk
        chunk, self._content = self._content[:size], self._content[size:]
        return chunk


class _Request:
    """上传端点只用到 headers，这里给一个空 headers 让 content-length 预检直接跳过。"""

    headers: dict = {}


def _patch_upload(monkeypatch, calls, failure):
    """把上传链路的外部依赖打桩，add_chunks 按 failure 抛错。"""

    def failing_add_chunks(*args, **kwargs):
        raise failure

    monkeypatch.setattr(
        knowledge_service,
        "resolve_knowledge_base",
        lambda db, kid, user_id: type("Base", (), {"id": 2})(),
    )
    monkeypatch.setattr(knowledge_service.crud_knowledge_file, "extract_file_text", lambda name, content: "第一段")
    monkeypatch.setattr(
        knowledge_service.crud_knowledge_file,
        "create_knowledge_file",
        lambda db, **kwargs: type("Entry", (), {"id": 11, "name": "考勤制度.txt"})(),
    )
    monkeypatch.setattr(
        knowledge_service.crud_knowledge_file,
        "chunk_text",
        lambda text, file_id: [{"id": "0", "text": text}],
    )
    monkeypatch.setattr(knowledge_service, "add_chunks", failing_add_chunks)
    monkeypatch.setattr(knowledge_service, "delete_file_chunks", lambda file_id: calls.append(("vectors", file_id)))
    monkeypatch.setattr(
        knowledge_service.crud_knowledge_file,
        "delete_knowledge_file",
        lambda db, file_id, user_id: calls.append(("mysql", file_id)),
    )


def _upload():
    return asyncio.run(
        knowledge_service.upload_knowledge(
            request=_Request(),
            file=_UploadFile("考勤制度.txt", b"text"),
            knowledge_base_id=2,
            user=_User(),
            db=object(),
        )
    )


def test_upload_knowledge_embedding_failure_returns_500_with_reason_and_cleans_up(monkeypatch):
    calls = []
    failure = milvus_client.EmbeddingBackendError(
        "向量化接口调用失败（https://embedding.example/v1/embeddings）：429 Too Many Requests"
    )
    _patch_upload(monkeypatch, calls, failure)

    with pytest.raises(HTTPException) as exc_info:
        _upload()

    assert exc_info.value.status_code == 500
    assert "向量化接口调用失败" in exc_info.value.detail
    assert "429" in exc_info.value.detail
    assert calls == [("vectors", 11), ("mysql", 11)]


def test_upload_knowledge_other_index_failure_keeps_generic_500(monkeypatch):
    calls = []
    _patch_upload(monkeypatch, calls, RuntimeError("milvus unavailable"))

    with pytest.raises(HTTPException) as exc_info:
        _upload()

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "知识文件上传失败，向量库写入异常。"
    assert calls == [("vectors", 11), ("mysql", 11)]


def test_upload_knowledge_warns_when_indexed_text_far_below_source(monkeypatch, caplog):
    """入库文本远少于原文时必须留痕，不能再静默返回成功。"""
    calls = []
    source = "第1条 " + "本制度适用于全体员工，由人事部负责解释与修订。" * 20
    _patch_upload(monkeypatch, calls, RuntimeError("unused"))
    monkeypatch.setattr(knowledge_service.crud_knowledge_file, "extract_file_text", lambda name, content: source)
    monkeypatch.setattr(
        knowledge_service.crud_knowledge_file,
        "chunk_text",
        lambda text, file_id: [{"id": "0", "text": text[:10]}],
    )
    monkeypatch.setattr(knowledge_service.crud_knowledge_file, "serialize_knowledge_file", lambda entry: {"id": entry.id})
    monkeypatch.setattr(knowledge_service, "add_chunks", lambda *args, **kwargs: calls.append(("indexed", args[1])))

    with caplog.at_level(logging.WARNING):
        result = _upload()

    assert result == {"id": 11}
    assert ("indexed", 11) in calls
    assert "chunk coverage too low" in caplog.text


def test_rebuild_existing_knowledge_index_warns_when_chunking_produces_nothing(monkeypatch, caplog):
    calls = []
    closed = []

    class FakeSession:
        def query(self, model):
            return self

        def filter(self, *args):
            return self

        def all(self):
            return [type("Entry", (), {"id": 1, "content": "第一段", "name": "文件1.txt", "knowledge_base_id": 2})()]

        def close(self):
            closed.append(True)

    monkeypatch.setattr(knowledge_service, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(knowledge_service.crud_knowledge_file, "chunk_text", lambda text, file_id: [])
    monkeypatch.setattr(knowledge_service, "add_chunks", lambda *args, **kwargs: calls.append(args[1]))

    with caplog.at_level(logging.WARNING):
        knowledge_service.rebuild_existing_knowledge_index()

    assert calls == [1]
    assert closed == [True]
    assert "chunk coverage too low" in caplog.text


def test_rebuild_existing_knowledge_index_continues_after_single_file_failure(monkeypatch):
    calls = []
    closed = []

    def _entry(file_id, content):
        return type("Entry", (), {"id": file_id, "content": content, "name": f"文件{file_id}.txt", "knowledge_base_id": 2})()

    class FakeSession:
        def query(self, model):
            return self

        def filter(self, *args):
            return self

        def all(self):
            return [_entry(1, "第一段"), _entry(2, "第二段")]

        def close(self):
            closed.append(True)

    def fake_add_chunks(chunks, file_id, file_name, knowledge_base_id):
        calls.append(file_id)
        if file_id == 1:
            raise milvus_client.EmbeddingBackendError("向量化接口调用失败：429 Too Many Requests")

    monkeypatch.setattr(knowledge_service, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(knowledge_service.crud_knowledge_file, "chunk_text", lambda text, file_id: [{"id": "0", "text": text}])
    monkeypatch.setattr(knowledge_service, "add_chunks", fake_add_chunks)

    knowledge_service.rebuild_existing_knowledge_index()

    assert calls == [1, 2]
    assert closed == [True]


class _User:
    id = 7


def test_delete_knowledge_cleans_vectors_before_mysql_delete(monkeypatch):
    calls = []

    monkeypatch.setattr(knowledge_service.crud_knowledge_file, "get_knowledge_file", lambda db, fid, user_id: object())
    monkeypatch.setattr(knowledge_service, "delete_file_chunks", lambda fid: calls.append(("vectors", fid)))
    monkeypatch.setattr(
        knowledge_service.crud_knowledge_file,
        "delete_knowledge_file",
        lambda db, fid, user_id: calls.append(("mysql", fid)),
    )

    assert knowledge_service.delete_knowledge(5, user=_User(), db=object()) == {"message": "ok"}
    assert calls == [("vectors", 5), ("mysql", 5)]


def test_delete_knowledge_keeps_mysql_when_vector_cleanup_fails(monkeypatch):
    calls = []

    def fail_vector_cleanup(fid):
        calls.append(("vectors", fid))
        raise RuntimeError("milvus unavailable")

    monkeypatch.setattr(knowledge_service.crud_knowledge_file, "get_knowledge_file", lambda db, fid, user_id: object())
    monkeypatch.setattr(knowledge_service, "delete_file_chunks", fail_vector_cleanup)
    monkeypatch.setattr(
        knowledge_service.crud_knowledge_file,
        "delete_knowledge_file",
        lambda db, fid, user_id: calls.append(("mysql", fid)),
    )

    with pytest.raises(HTTPException) as exc_info:
        knowledge_service.delete_knowledge(5, user=_User(), db=object())

    assert exc_info.value.status_code == 500
    assert calls == [("vectors", 5)]


def test_delete_knowledge_base_cleans_vectors_before_mysql_delete(monkeypatch):
    calls = []

    monkeypatch.setattr(knowledge_service.crud_knowledge_base, "get_knowledge_base", lambda db, kid, user_id: object())
    monkeypatch.setattr(knowledge_service.crud_knowledge_base, "count_knowledge_bases", lambda db, user_id: 2)
    monkeypatch.setattr(
        knowledge_service.crud_knowledge_base,
        "get_fallback_knowledge_base",
        lambda db, deleted_id, user_id: type("Base", (), {"id": 99})(),
    )
    monkeypatch.setattr(
        knowledge_service.crud_knowledge_base,
        "list_files_for_knowledge_base",
        lambda db, kid: [_FileEntry(7), _FileEntry(8)],
    )
    monkeypatch.setattr(knowledge_service, "delete_file_chunks", lambda file_id: calls.append(("vectors", file_id)))
    monkeypatch.setattr(
        knowledge_service.crud_knowledge_base,
        "delete_knowledge_base_with_files",
        lambda db, kid, fallback_id, user_id: calls.append(("mysql", kid, fallback_id)),
    )

    result = knowledge_service.delete_knowledge_base(3, user=_User(), db=object())

    assert result == {"message": "ok", "fallback_knowledge_base_id": 99}
    assert calls == [("vectors", 7), ("vectors", 8), ("mysql", 3, 99)]


def test_delete_knowledge_base_keeps_mysql_when_vector_cleanup_fails(monkeypatch):
    calls = []

    def fail_vector_cleanup(file_id):
        calls.append(("vectors", file_id))
        raise RuntimeError("milvus unavailable")

    monkeypatch.setattr(knowledge_service.crud_knowledge_base, "get_knowledge_base", lambda db, kid, user_id: object())
    monkeypatch.setattr(knowledge_service.crud_knowledge_base, "count_knowledge_bases", lambda db, user_id: 2)
    monkeypatch.setattr(
        knowledge_service.crud_knowledge_base,
        "get_fallback_knowledge_base",
        lambda db, deleted_id, user_id: type("Base", (), {"id": 99})(),
    )
    monkeypatch.setattr(
        knowledge_service.crud_knowledge_base,
        "list_files_for_knowledge_base",
        lambda db, kid: [_FileEntry(7)],
    )
    monkeypatch.setattr(knowledge_service, "delete_file_chunks", fail_vector_cleanup)
    monkeypatch.setattr(
        knowledge_service.crud_knowledge_base,
        "delete_knowledge_base_with_files",
        lambda db, kid, fallback_id, user_id: calls.append(("mysql", kid, fallback_id)),
    )

    with pytest.raises(HTTPException) as exc_info:
        knowledge_service.delete_knowledge_base(3, user=_User(), db=object())

    assert exc_info.value.status_code == 500
    assert calls == [("vectors", 7)]
