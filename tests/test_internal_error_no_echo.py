"""回归（issue #96）：内部异常文本不得经 SSE 帧或 HTTP detail 回显给用户。

issue 复现的两条泄漏通路：

1. 学习轨迹 —— `result={"error": str(exc)}` 经 `drain_sse_payloads()` → `_trace_sse_payloads()`
   编成 `text/event-stream` 帧推给浏览器；同一份文本又随 `retrieval_trace` 落库，之后由
   `get_message_trace()` / `get_chat_trace()` 再查出来返回。
2. 上传/解析 —— 异常文本直接拼进 `HTTPException` 的 detail。

本文件按「测试内注入」构造会抛内部异常的分支，断言用户侧负载不含异常原文，也不含
`OperationalError` / `sqlite3` 这类可据以推断后端实现的字样；同时断言服务端仍留下可诊断的
日志与用户可报出的编号（验收标准第 3 条：可诊断性不降级）。

SSE 用例跑真实的 `TraceRecorder` + 真实的 `sanitize_trace_value` + 真实的帧编码，只打桩落库，
避免替身把要验证的那段逻辑一并替换掉；否则「帧里没有异常原文」可能只是替身不复现编码路径。
另外 `sanitize_trace_value()` 并不掩码 `error` 键（issue 已指出），所以帧干净只能来自源头写入
固定文案，而不是靠掩码兜底——这正是要被锁住的性质。
"""

import asyncio
import logging

import docx
import pypdf
import pytest
from fastapi import HTTPException

import rag.learning_trace as learning_trace
import rag.llm as llm
from conftest import FakeKnowledgeBase
from crud import knowledge_file
from schema.schemas import ChatRequest
from service import chat_service


# 与 issue 复现步骤同形的内部异常文本：数据库驱动报错 + 表列名。
INTERNAL_ERROR_TEXT = "sqlite3.OperationalError: no such column: chat_messages.secret_col"
# 这些字样只要出现在用户侧负载里，就说明异常原文（或后端实现细节）回显了。
LEAK_MARKERS = ("OperationalError", "sqlite3", "chat_messages.secret_col")

CHUNK = {"file_name": "制度.txt", "content": "迟到规则", "file_id": 1, "chunk_id": "a"}


class _StubUploadFile:
    """UploadFile 的最小替身：`upload_chat_attachment` 只用到这三个成员。"""

    def __init__(self, content: bytes = b"png-bytes", filename: str = "a.png",
                 content_type: str = "image/png"):
        self._content = content
        self.filename = filename
        self.content_type = content_type

    async def read(self):
        return self._content


def _assert_no_leak(text: str, where: str) -> None:
    for marker in LEAK_MARKERS:
        assert marker not in text, f"{where} 回显了内部异常文本片段 {marker!r}：{text}"


def _assert_sse_frames_clean(body: str, where: str) -> None:
    """逐帧断言，失败时只回显泄漏的那一帧，不把整条流倒进测试输出。"""
    leaked = [
        frame.strip()
        for frame in body.split("\n\n")
        if any(marker in frame for marker in LEAK_MARKERS)
    ]
    assert not leaked, f"{where} 回显了内部异常文本：{leaked[0]}"


def _boom(*_args, **_kwargs):
    raise RuntimeError(INTERNAL_ERROR_TEXT)


async def _async_boom(*_args, **_kwargs):
    raise RuntimeError(INTERNAL_ERROR_TEXT)


class _RealTrace:
    """收集真实 TraceRecorder 的事件与实例，供用例断言事件负载与 trace_id。"""

    def __init__(self):
        self.events: list[dict] = []
        self.instances: list = []


@pytest.fixture
def real_trace(monkeypatch):
    """启用真实 TraceRecorder，只打桩落库。"""
    monkeypatch.setattr(learning_trace, "LEARNING_TRACE_ENABLED", True)
    monkeypatch.setattr(learning_trace.TraceRecorder, "_persist", lambda self, **kwargs: None)
    collector = _RealTrace()
    original_init = learning_trace.TraceRecorder.__init__
    original_add = learning_trace.TraceRecorder.add

    def spy_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        collector.instances.append(self)

    def spy_add(self, *args, **kwargs):
        event = original_add(self, *args, **kwargs)
        if event:
            collector.events.append(event)
        return event

    monkeypatch.setattr(learning_trace.TraceRecorder, "__init__", spy_init)
    monkeypatch.setattr(learning_trace.TraceRecorder, "add", spy_add)
    return collector


def _patch_stream_boundaries(monkeypatch, fake_db):
    """只打桩真正的边界（鉴权/会话工厂/知识库解析/检索/模型），其余跑真实实现。"""
    monkeypatch.setattr(chat_service, "decode_token", lambda authorization: "alice")
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: fake_db)
    monkeypatch.setattr(chat_service, "resolve_knowledge_base", lambda db, kid, user_id: FakeKnowledgeBase())

    async def fake_build_effective_question(question, attachments):
        return question, {"status": "skipped"}

    async def fake_recent_memory_text(*_args, **_kwargs):
        return ""

    async def fake_decide_need_rag(*_args, **_kwargs):
        return {"need_rag": True, "route": "rag", "confidence": 1.0, "source": "test", "reason": "needs retrieval"}

    async def fake_retrieve_knowledge(question, knowledge_base_id, db, trace_recorder=None):
        return (
            [dict(CHUNK)],
            {"query_plan": {}, "routes": [], "rrf": [], "rerank": {"status": "done", "items": []}},
        )

    monkeypatch.setattr(chat_service, "_build_effective_question", fake_build_effective_question)
    monkeypatch.setattr(chat_service, "_build_recent_memory_text", fake_recent_memory_text)
    monkeypatch.setattr(chat_service, "decide_need_rag", fake_decide_need_rag)
    monkeypatch.setattr(chat_service, "retrieve_knowledge", fake_retrieve_knowledge)

    async def real_chain(question, context, memory_context, trace, use_rag=True):
        async for event in llm.stream_answer_events(question, context, memory_context, trace, use_rag):
            yield event

    monkeypatch.setattr(chat_service, "stream_rag_answer", real_chain)
    monkeypatch.setattr(llm, "DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "TEXT_FALLBACK_API_KEY", "sk-test")

    async def fake_stream(model, messages):
        for piece in ["根据《员工手册》", "迟到30分钟以内罚款50元。"]:
            yield piece

    monkeypatch.setattr(llm, "_stream_model_chunks", fake_stream)


def _run_stream(fake_db) -> str:
    """驱动真实 stream_chat，把全部 SSE 帧拼成文本。"""

    async def _run():
        response = await chat_service.stream_chat(
            ChatRequest(question="迟到30分钟以内怎么罚款？"), authorization="Bearer token"
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    return asyncio.run(_run())


def _stage_result(events, stage):
    for event in events:
        if event.get("stage") == stage:
            return event.get("result") or {}
    raise AssertionError(f"轨迹里没有 {stage} 事件：{[e.get('stage') for e in events]}")


def _fail_assistant_save(monkeypatch, fake_db):
    """只在 assistant 消息落库那一次 commit 上抛异常，模拟保存失败。"""
    original_commit = fake_db.commit

    def failing_commit():
        if fake_db.added_by_role("assistant"):
            raise RuntimeError(INTERNAL_ERROR_TEXT)
        original_commit()

    monkeypatch.setattr(fake_db, "commit", failing_commit)


# --- 学习轨迹：SSE 帧（chat_service 三处 result={"error": str(exc)}） --------


def test_assistant_save_failure_sse_frame_hides_internal_error(monkeypatch, fake_db, real_trace):
    """assistant 消息保存失败：帧里只应有固定文案与可对上的 trace_id。"""
    _patch_stream_boundaries(monkeypatch, fake_db)
    _fail_assistant_save(monkeypatch, fake_db)

    body = _run_stream(fake_db)

    _assert_sse_frames_clean(body, "SSE 帧")
    result = _stage_result(real_trace.events, "assistant_save_failed")
    assert result["error"] == chat_service.ASSISTANT_SAVE_FAILED_MESSAGE
    assert result["trace_id"] == real_trace.instances[-1].trace_id


def test_ragas_schedule_failure_sse_frame_hides_internal_error(monkeypatch, fake_db, real_trace):
    """RAGAS 调度失败：帧里不含异常原文。"""
    _patch_stream_boundaries(monkeypatch, fake_db)
    monkeypatch.setattr(chat_service, "schedule_ragas_evaluation", _boom)

    body = _run_stream(fake_db)

    _assert_sse_frames_clean(body, "SSE 帧")
    result = _stage_result(real_trace.events, "ragas_schedule_failed")
    assert result["error"] == chat_service.RAGAS_SCHEDULE_FAILED_MESSAGE
    assert result["trace_id"] == real_trace.instances[-1].trace_id


def test_memory_summary_schedule_failure_sse_frame_hides_internal_error(monkeypatch, fake_db, real_trace):
    """长期记忆压缩调度失败：帧里不含异常原文。"""
    _patch_stream_boundaries(monkeypatch, fake_db)
    monkeypatch.setattr(chat_service, "_schedule_memory_summary_update", _boom)

    body = _run_stream(fake_db)

    _assert_sse_frames_clean(body, "SSE 帧")
    result = _stage_result(real_trace.events, "memory_summary_update_schedule_failed")
    assert result["error"] == chat_service.MEMORY_SUMMARY_SCHEDULE_FAILED_MESSAGE
    assert result["trace_id"] == real_trace.instances[-1].trace_id


def test_failure_branch_still_logs_exception_with_traceback(monkeypatch, fake_db, real_trace, caplog):
    """可诊断性不降级：异常原文仍以 exc_info 落服务端日志，只是不再回显给用户。"""
    _patch_stream_boundaries(monkeypatch, fake_db)
    _fail_assistant_save(monkeypatch, fake_db)

    with caplog.at_level(logging.WARNING):
        _run_stream(fake_db)

    assert INTERNAL_ERROR_TEXT in caplog.text
    assert "Assistant message save failed after stream finished" in caplog.text


def test_sse_trace_id_is_greppable_in_server_log(monkeypatch, fake_db, real_trace, caplog):
    """用户从帧里看到 trace_id 后，必须能在服务端日志中按它检索到这次失败。"""
    _patch_stream_boundaries(monkeypatch, fake_db)
    _fail_assistant_save(monkeypatch, fake_db)

    with caplog.at_level(logging.WARNING):
        _run_stream(fake_db)

    trace_id = real_trace.instances[-1].trace_id
    assert trace_id in caplog.text
    assert f"[trace_id={trace_id}]" in caplog.text


def test_normal_stream_still_succeeds(monkeypatch, fake_db, real_trace):
    """回归：不注入异常时，正常路径照旧产出回答，也不产生失败事件。"""
    _patch_stream_boundaries(monkeypatch, fake_db)

    body = _run_stream(fake_db)

    assert "迟到30分钟以内罚款50元。" in body
    assert "assistant_save_failed" not in body
    assert "ragas_schedule_failed" not in body
    assert "memory_summary_update_schedule_failed" not in body


# --- HTTP detail（上传 / 解析三处） -----------------------------------------


def test_oss_upload_failure_detail_hides_internal_error(monkeypatch, caplog):
    """OSS 上传失败：500 detail 只给固定文案 + 编号，原文进日志。"""
    monkeypatch.setattr(chat_service, "_put_oss_object", _async_boom)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(chat_service.upload_chat_attachment(_StubUploadFile(), _user=None))

    assert excinfo.value.status_code == 500
    _assert_no_leak(str(excinfo.value.detail), "OSS 上传 500 detail")
    assert excinfo.value.detail.startswith(chat_service.OSS_UPLOAD_FAILED_MESSAGE)


def test_docx_parse_failure_detail_hides_internal_error(monkeypatch):
    """DOCX 解析失败：400 detail 只给固定文案 + 编号。"""
    monkeypatch.setattr(docx, "Document", _boom)

    with pytest.raises(HTTPException) as excinfo:
        knowledge_file.extract_docx_text(b"not-a-docx")

    assert excinfo.value.status_code == 400
    _assert_no_leak(str(excinfo.value.detail), "DOCX 解析 400 detail")
    assert excinfo.value.detail.startswith(knowledge_file.DOCX_PARSE_FAILED_MESSAGE)


def test_pdf_parse_failure_detail_hides_internal_error(monkeypatch):
    """PDF 解析失败：400 detail 只给固定文案 + 编号。"""
    monkeypatch.setattr(pypdf, "PdfReader", _boom)

    with pytest.raises(HTTPException) as excinfo:
        knowledge_file.extract_pdf_text(b"not-a-pdf")

    assert excinfo.value.status_code == 400
    _assert_no_leak(str(excinfo.value.detail), "PDF 解析 400 detail")
    assert excinfo.value.detail.startswith(knowledge_file.PDF_PARSE_FAILED_MESSAGE)


def test_http_detail_error_id_matches_server_log(monkeypatch, caplog):
    """用户拿到的「错误编号」必须能在服务端日志里对上，否则排障无从谈起。"""
    monkeypatch.setattr(chat_service, "_put_oss_object", _async_boom)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(chat_service.upload_chat_attachment(_StubUploadFile(), _user=None))

    detail = str(excinfo.value.detail)
    error_id = detail.split("错误编号：", 1)[1].rstrip("）")
    assert error_id, f"detail 里没有错误编号：{detail}"
    assert f"error_id={error_id}" in caplog.text
    assert INTERNAL_ERROR_TEXT in caplog.text


def test_parse_failure_still_logs_original_exception(monkeypatch, caplog):
    """解析失败的原文同样只落日志，不外泄、也不丢失。"""
    monkeypatch.setattr(docx, "Document", _boom)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(HTTPException):
            knowledge_file.extract_docx_text(b"not-a-docx")

    assert INTERNAL_ERROR_TEXT in caplog.text
    assert "docx_parse failed" in caplog.text
