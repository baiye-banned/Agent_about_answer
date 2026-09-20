import asyncio
import threading
import time

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


def test_upload_knowledge_indexes_off_the_event_loop_thread_and_keeps_heartbeats(monkeypatch):
    """入库期间事件循环仍在调度心跳协程，且同步入库确实跑在别的线程上。

    修复前 add_chunks 是同步调用：整份文档向量化加写库期间事件循环被独占，
    同一循环上的心跳协程一次都排不上（这里会是 0）。
    """
    calls = []
    delay = 0.3
    _patch_upload(monkeypatch, calls, RuntimeError("unused"))
    # 成功路径要序列化返回体，桩 entry 必须带齐序列化用到的字段。
    entry = type(
        "Entry",
        (),
        {"id": 11, "name": "考勤制度.txt", "knowledge_base_id": 2, "size": 4, "created_at": None},
    )()
    monkeypatch.setattr(knowledge_service.crud_knowledge_file, "create_knowledge_file", lambda db, **kwargs: entry)

    def slow_add_chunks(chunks, file_id, file_name, knowledge_base_id):
        calls.append({"thread": threading.get_ident(), "chunk_count": len(chunks), "file_id": file_id})
        time.sleep(delay)

    monkeypatch.setattr(knowledge_service, "add_chunks", slow_add_chunks)

    async def scenario():
        heartbeats = []

        async def ticker():
            while True:
                await asyncio.sleep(0.02)
                heartbeats.append(time.perf_counter())

        ticker_task = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)  # 先让心跳协程真正跑起来，再开始计时
        started_at = time.perf_counter()
        result = await knowledge_service.upload_knowledge(
            request=_Request(),
            file=_UploadFile("考勤制度.txt", b"text"),
            knowledge_base_id=2,
            user=_User(),
            db=object(),
        )
        finished_at = time.perf_counter()
        ticker_task.cancel()
        during = [beat for beat in heartbeats if started_at < beat < finished_at]
        return during, finished_at - started_at, result

    heartbeats_during_upload, elapsed, result = asyncio.run(scenario())

    assert result["id"] == 11
    assert len(calls) == 1
    assert calls[0]["file_id"] == 11
    # 入库期间事件循环仍在调度心跳协程：修复前这里是 0（同步入库独占事件循环）。
    assert heartbeats_during_upload
    # 同步入库被移出了事件循环所在线程，且是被等待完成的（不是发出去就不管）。
    assert calls[0]["thread"] != threading.get_ident()
    assert elapsed >= delay


def test_startup_rebuild_runs_off_the_event_loop(monkeypatch):
    """启动重建同样不独占事件循环：lifespan 执行期间心跳协程仍被调度。"""
    import main

    calls = []
    delay = 0.3

    def slow_rebuild():
        calls.append(threading.get_ident())
        time.sleep(delay)

    monkeypatch.setattr(main, "ensure_secret_key_configured", lambda: None)
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "seed_default_users", lambda: None)
    monkeypatch.setattr(main, "REBUILD_KNOWLEDGE_INDEX_ON_STARTUP", True)
    monkeypatch.setattr(main, "rebuild_existing_knowledge_index", slow_rebuild)

    async def scenario():
        heartbeats = []

        async def ticker():
            while True:
                await asyncio.sleep(0.02)
                heartbeats.append(time.perf_counter())

        ticker_task = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        started_at = time.perf_counter()
        async with main.lifespan(main.app):
            pass
        finished_at = time.perf_counter()
        ticker_task.cancel()
        during = [beat for beat in heartbeats if started_at < beat < finished_at]
        return during, finished_at - started_at

    heartbeats_during_rebuild, elapsed = asyncio.run(scenario())

    # 重建期间事件循环仍在调度心跳协程：修复前这里是 0（同步重建独占事件循环）。
    assert heartbeats_during_rebuild
    assert calls
    assert elapsed >= delay
    assert calls[0] != threading.get_ident()
