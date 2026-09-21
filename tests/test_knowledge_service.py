import asyncio
import io
import logging
import os
import threading
import time

import pytest
from docx import Document
from fastapi import HTTPException

from crud import knowledge_file as crud_knowledge_file
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


def _serializable_entry(file_id=11, name="考勤制度.txt"):
    """成功路径要序列化返回体，桩 entry 必须带齐 serialize_knowledge_file 用到的字段。"""
    return type(
        "Entry",
        (),
        {"id": file_id, "name": name, "knowledge_base_id": 2, "size": 4, "created_at": None},
    )()


def _docx_payload(paragraphs):
    document = Document()
    for index in range(paragraphs):
        document.add_paragraph(f"第{index}条 员工迟到{index}分钟以内罚款50元，由人事部汇总")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_upload_knowledge_parses_docx_off_the_event_loop(monkeypatch):
    """issue #83 第 3 项：.docx 解析与分块都离开事件循环线程。

    先证红：修复前 extract_file_text / chunk_text 是在协程里直接同步调用的，
    解析 1500 段文档的 ~150ms 里心跳协程一次都排不上，且两个调用都发生在事件循环线程上。
    上面那条心跳用例把 extract_file_text / chunk_text 都打了桩，够不到这一段。
    """
    real_extract_file_text = crud_knowledge_file.extract_file_text
    real_extract_docx_text = crud_knowledge_file.extract_docx_text
    real_chunk_text = crud_knowledge_file.chunk_text
    payload = _docx_payload(1500)
    calls = []
    seen = {}

    _patch_upload(monkeypatch, calls, RuntimeError("unused"))
    # 恢复真实的抽取链，只在外面套一层记录线程的壳。
    monkeypatch.setattr(crud_knowledge_file, "extract_file_text", real_extract_file_text)

    def recording_extract_docx(content):
        seen["parse_thread"] = threading.get_ident()
        time.sleep(0.2)  # 把解析耗时钉死，不依赖机器速度
        return real_extract_docx_text(content)

    def recording_chunk_text(text, file_id):
        seen["chunk_thread"] = threading.get_ident()
        time.sleep(0.2)
        return real_chunk_text(text, file_id)

    monkeypatch.setattr(crud_knowledge_file, "extract_docx_text", recording_extract_docx)
    monkeypatch.setattr(crud_knowledge_file, "chunk_text", recording_chunk_text)
    monkeypatch.setattr(
        crud_knowledge_file, "create_knowledge_file", lambda db, **kwargs: _serializable_entry()
    )
    # 本用例只关心解析与分块线程：向量化桩成空操作，走成功路径。
    monkeypatch.setattr(knowledge_service, "add_chunks", lambda *args, **kwargs: None)

    async def scenario():
        heartbeats = []

        async def ticker():
            while True:
                await asyncio.sleep(0.02)
                heartbeats.append(time.perf_counter())

        loop_thread = threading.get_ident()
        ticker_task = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        started_at = time.perf_counter()
        result = await knowledge_service.upload_knowledge(
            request=_Request(),
            file=_UploadFile("考勤制度.docx", payload, content_type=None),
            knowledge_base_id=2,
            user=_User(),
            db=object(),
        )
        finished_at = time.perf_counter()
        ticker_task.cancel()
        return [beat for beat in heartbeats if started_at < beat < finished_at], result, loop_thread

    heartbeats_during_upload, result, loop_thread = asyncio.run(scenario())

    assert result["id"] == 11
    # 解析与分块都真的跑了（没有被桩挡掉），且都不在事件循环线程上。
    assert set(seen) == {"parse_thread", "chunk_thread"}
    assert seen["parse_thread"] != loop_thread
    assert seen["chunk_thread"] != loop_thread
    # 解析与分块期间事件循环仍在调度心跳协程：修复前这里是 0。
    assert heartbeats_during_upload


def test_upload_knowledge_ingests_without_queuing_behind_a_saturated_default_executor(monkeypatch):
    """issue #83 第 4 项：入库走独立线程池，不与检索抢 asyncio 默认池。

    先证红：修复前 add_chunks 通过 asyncio.to_thread 提交到**默认** executor，
    把默认池的全部槽位占满（模拟并发检索/上传把池吃满）之后，入库请求只能排队，
    这里的 wait_for 会超时。修复后入库有自己的池，默认池再忙也影响不到它。
    """
    # asyncio 默认 executor 的容量就是 concurrent.futures 的默认值。
    default_slots = min(32, (os.cpu_count() or 1) + 4)
    calls = []
    ingest_threads = []
    _patch_upload(monkeypatch, calls, RuntimeError("unused"))
    monkeypatch.setattr(
        crud_knowledge_file, "create_knowledge_file", lambda db, **kwargs: _serializable_entry()
    )
    monkeypatch.setattr(
        knowledge_service, "add_chunks", lambda *args, **kwargs: ingest_threads.append(threading.get_ident())
    )

    async def scenario():
        loop = asyncio.get_running_loop()
        gate = threading.Event()
        started = 0
        started_lock = threading.Lock()
        all_started = asyncio.Event()

        def blocker():
            nonlocal started
            with started_lock:
                started += 1
                if started >= default_slots:
                    loop.call_soon_threadsafe(all_started.set)
            gate.wait(10)

        blockers = [asyncio.ensure_future(asyncio.to_thread(blocker)) for _ in range(default_slots)]
        await asyncio.wait_for(all_started.wait(), timeout=10)
        try:
            return await asyncio.wait_for(
                knowledge_service.upload_knowledge(
                    request=_Request(),
                    file=_UploadFile("考勤制度.txt", b"text"),
                    knowledge_base_id=2,
                    user=_User(),
                    db=object(),
                ),
                timeout=5,
            )
        finally:
            gate.set()
            await asyncio.gather(*blockers, return_exceptions=True)

    result = asyncio.run(scenario())

    assert result["id"] == 11
    assert len(ingest_threads) == 1


class _CleanupSession:
    """超时清理用的独立 Session 桩：交出句柄并记录关闭，不碰真实数据库。

    实际删行走 crud_knowledge_file.delete_knowledge_file，那是 _patch_upload 已打桩的
    既有清理出口（记录成 ("mysql", fid)），这里只要保证它拿到的是一个独立 Session。
    """

    def __init__(self, log):
        self.log = log

    def close(self):
        self.log.append(("db-close",))


def test_upload_knowledge_times_out_when_ingest_exceeds_the_document_budget(monkeypatch):
    """issue #83 第 4 项：整份文档有总时限，超时按 500 收口。

    先证红：修复前只有 EMBEDDING_INGEST_TIMEOUT_SECONDS 的单批超时，一份文档要发多批，
    没有总量约束——stuck 的向量化会把上传请求永久挂住。

    清理必须排在写入线程**之后**（对抗评审实测的时序）：线程取消不掉，
    先删向量再等它写完，就会留下「列表里没有、检索却命中」的孤儿向量。
    """
    calls = []
    release = threading.Event()
    writer_wrote = threading.Event()
    entry = _serializable_entry()
    _patch_upload(monkeypatch, calls, RuntimeError("unused"))
    monkeypatch.setattr(crud_knowledge_file, "create_knowledge_file", lambda db, **kwargs: entry)
    monkeypatch.setattr(knowledge_service, "SessionLocal", lambda: _CleanupSession(calls))

    def stuck_add_chunks(chunks, file_id, *_args):
        calls.append(("indexing-start", file_id))
        release.wait(10)
        # 模拟「请求已放弃、写入线程仍把这一批写进向量库」。
        calls.append(("indexing-wrote-vectors", file_id))
        writer_wrote.set()

    monkeypatch.setattr(knowledge_service, "add_chunks", stuck_add_chunks)
    monkeypatch.setattr(knowledge_service, "KNOWLEDGE_INDEX_TOTAL_TIMEOUT_SECONDS", 0.2, raising=False)

    try:
        with pytest.raises(HTTPException) as exc_info:
            _upload()
        # 请求返回的那一刻，清理一次都还没跑：此时写入线程还在写。
        assert calls == [("indexing-start", 11)]
        # 放行写入线程，等它把向量写完。
        release.set()
        assert writer_wrote.wait(10)
    finally:
        release.set()

    deadline = time.perf_counter() + 10
    while time.perf_counter() < deadline and ("vectors", 11) not in calls:
        time.sleep(0.01)

    assert exc_info.value.status_code == 500
    assert "超过总时限" in exc_info.value.detail
    # 500 的文案必须是「超过总时限」，不能落到通用失败分支。
    # Python 3.10 上 asyncio.TimeoutError 不是内建 TimeoutError，写错 except 就会
    # 悄悄走通用分支（CI 实测），这里把文案钉住，让那种回退过不了门禁。
    assert exc_info.value.detail == knowledge_service.INGEST_TIMEOUT_MESSAGE
    # 关键断言：向量写入发生在删除之前。反过来就是评审复现出的孤儿向量。
    assert ("indexing-wrote-vectors", 11) in calls
    assert calls.index(("indexing-wrote-vectors", 11)) < calls.index(("vectors", 11))
    # 删完向量再删元数据行，且用的是独立 Session（请求级 Session 早已随响应结束）。
    assert calls.index(("vectors", 11)) < calls.index(("mysql", 11))
    assert ("db-close",) in calls


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
