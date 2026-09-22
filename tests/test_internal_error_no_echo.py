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
import json
import logging
from types import SimpleNamespace

import docx
import httpx
import pypdf
import pytest
from fastapi import HTTPException

import rag.learning_trace as learning_trace
import rag.llm as llm
from conftest import FakeKnowledgeBase
from crud import knowledge_file
from crud import chat as crud_chat
from model.models import Message
from rag import milvus_client, vision_service
from schema.schemas import ChatRequest
from service import chat_service, oss_service


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


class _StubDb:
    """Session 的最小替身。

    上传链路在写对象**之前**要先落一条待确认行（issue #142），所以直接调 service 函数的
    用例也得给一个会话；这里只需要记录 add/commit，不落任何真实数据。
    """

    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, item):
        self.added.append(item)

    def commit(self):
        self.commits += 1


def _upload_call_kwargs():
    """`upload_chat_attachment` 的直调参数（绕开 FastAPI 依赖注入）。"""
    return {"user": SimpleNamespace(id=1), "db": _StubDb()}


def _assert_no_leak(text: str, where: str, markers=LEAK_MARKERS) -> None:
    for marker in markers:
        assert marker not in text, f"{where} 回显了内部异常文本片段 {marker!r}：{text}"


def _assert_sse_frames_clean(body: str, where: str, markers=LEAK_MARKERS) -> None:
    """逐帧断言，失败时只回显泄漏的那一帧，不把整条流倒进测试输出。"""
    leaked = [
        frame.strip()
        for frame in body.split("\n\n")
        if any(marker in frame for marker in markers)
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
            asyncio.run(chat_service.upload_chat_attachment(_StubUploadFile(), **_upload_call_kwargs()))

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
            asyncio.run(chat_service.upload_chat_attachment(_StubUploadFile(), **_upload_call_kwargs()))

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


# --- 图片分析：SSE 帧（vision_service 的失败分支） ---------------------------
#
# 同一判据、同一条通路：`_analyze_image_attachments` 的 `error` 字段被 `chat_service`
# 写进三段用户可见负载——`effective_question_built` 的 result、`image_failed_directly`
# 的 result、以及 `{"type":"error"}` 事件的 message，前端对两者都原样渲染。传输出错时
# 旧实现写的是 `str(exc)`，其中「上游服务地址」正是 issue #96 点名的一类文本。
#
# 两条复现都**不替换被测函数**：只驱动真实的 `stream_chat`，并让真实的 httpx 去处理
# 真实的 URL/响应，避免替身把要验证的那段逻辑一并替换掉。

OSS_CONFIG = {
    "OSS_ACCESS_KEY_ID": "test-id",
    "OSS_ACCESS_KEY_SECRET": "test-secret",
    "OSS_BUCKET": "demo",
    "OSS_ENDPOINT": "https://oss-cn-hangzhou.aliyuncs.com",
}
# 键必须是铸造形态（照抄上传路径铸出来的样子）：issue #181 之后出网口与写库口、删除口
# 共用同一条形态判据，非自铸键到不了 `_request_image_description`，本节要验的
# 「异常原文不进帧」就无从触发。键的形状本身由 tests/test_vision_outbound_guard_181.py 覆盖。
IMAGE_ATTACHMENT = {
    "object_key": "rag-chat/2026/09/21/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png",
    "file_name": "考勤.png",
    "content_type": "image/png",
}


def _patch_real_vision(monkeypatch):
    """让 stream_chat 走真实的 `vision_service._build_effective_question`。"""
    for name, value in OSS_CONFIG.items():
        monkeypatch.setattr(oss_service, name, value)
    monkeypatch.setattr(chat_service, "_build_effective_question", vision_service._build_effective_question)
    monkeypatch.setattr(vision_service, "VISION_API_KEY", "sk-vision")


def _run_attachment_stream(fake_db) -> str:
    """只发一张图、不带文字问题：走 `image_failed_directly` 那条失败流。"""

    async def _run():
        response = await chat_service.stream_chat(
            ChatRequest(question="", attachments=[dict(IMAGE_ATTACHMENT)]), authorization="Bearer token"
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    return asyncio.run(_run())


def _error_id_in(body: str) -> str:
    assert "错误编号：" in body, f"用户侧负载里没有可上报的错误编号：{body}"
    return body.split("错误编号：", 1)[1].split("）", 1)[0]


def test_image_analysis_invalid_url_sse_frames_hide_internal_error(monkeypatch, fake_db, real_trace, caplog):
    """视觉接口地址非法：帧里不得出现由该地址派生的异常文本。

    `httpx.InvalidURL` 不是 `httpx.HTTPError` 子类（`InvalidURL(Exception)` vs
    `HTTPError(TransportError)`），因此它逃出 `_request_image_description` 的窄
    `except httpx.HTTPError`，落到 `_analyze_image_attachments` 的 `except Exception`。
    """
    _patch_stream_boundaries(monkeypatch, fake_db)
    _patch_real_vision(monkeypatch)
    # 端口非数字：httpx 在解析阶段就抛 InvalidURL，不做任何网络 I/O。
    monkeypatch.setattr(vision_service, "VISION_BASE_URL", "https://vision.example.com:abc/v1")

    with caplog.at_level(logging.WARNING):
        body = _run_attachment_stream(fake_db)

    _assert_sse_frames_clean(body, "图片分析失败 SSE 帧", markers=("Invalid port", "InvalidURL", "httpx"))
    error_id = _error_id_in(body)
    # 可诊断性不降级：原文仍进日志，且用户看到的编号能在日志里对上。
    assert "Invalid port" in caplog.text
    assert f"error_id={error_id}" in caplog.text


def test_image_analysis_non_json_response_hides_internal_error(monkeypatch, fake_db, real_trace, caplog):
    """视觉接口返回 200 但响应体不是 JSON：`JSONDecodeError` 原文不得出现在帧里。"""
    _patch_stream_boundaries(monkeypatch, fake_db)
    _patch_real_vision(monkeypatch)
    monkeypatch.setattr(vision_service, "VISION_BASE_URL", "https://vision.example.com/v1")

    class _NonJsonResponse:
        status_code = 200
        text = "<html>502 Bad Gateway</html>"

        @staticmethod
        def json():
            raise json.JSONDecodeError("Expecting value", _NonJsonResponse.text, 0)

    class _NonJsonClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, *_args, **_kwargs):
            return _NonJsonResponse()

    monkeypatch.setattr(vision_service.httpx, "AsyncClient", _NonJsonClient)

    with caplog.at_level(logging.WARNING):
        body = _run_attachment_stream(fake_db)

    _assert_sse_frames_clean(body, "图片分析失败 SSE 帧", markers=("Expecting value", "JSONDecodeError"))
    error_id = _error_id_in(body)
    assert "Expecting value" in caplog.text
    assert f"error_id={error_id}" in caplog.text


def test_image_analysis_failure_keeps_user_facing_message_and_status(monkeypatch, fake_db, real_trace):
    """回归：失败仍是 failed 状态、仍给出可读文案，只是不再附异常原文。"""
    _patch_stream_boundaries(monkeypatch, fake_db)
    _patch_real_vision(monkeypatch)
    monkeypatch.setattr(vision_service, "VISION_BASE_URL", "https://vision.example.com:abc/v1")

    body = _run_attachment_stream(fake_db)

    analysis = [
        json.loads(frame[len("data: "):])
        for frame in body.split("\n\n")
        if frame.strip().startswith("data: {")
    ]
    events = [event for event in analysis if event.get("type") != "trace"]
    image_event = next(event for event in events if event.get("type") == "image_analysis")
    error_event = next(event for event in events if event.get("type") == "error")

    assert image_event["analysis"]["status"] == "failed"
    assert image_event["analysis"]["error"].startswith(vision_service.IMAGE_ANALYSIS_FAILED_MESSAGE)
    assert error_event["message"].startswith(vision_service.IMAGE_ANALYSIS_FAILED_MESSAGE)
    result = _stage_result(real_trace.events, "image_failed_directly")
    assert result["error"] == image_event["analysis"]["error"]


def test_image_analysis_exception_hides_internal_error(monkeypatch, fake_db, real_trace, caplog):
    """与 `test_llm_text_fallback_failure_hides_internal_error` 同形：在边界上注入任意内部异常，
    用户侧负载只剩固定文案 + 可上报编号，原文仍进日志。

    上面两条走的是真实 httpx 的两类具体报错（地址非法 / 响应非 JSON），本条的用意不同：
    它锁定的是「外层 `except Exception` 这条兜底通路本身」，与异常类无关——将来任何新异常
    类型从 `_request_image_description` 逃出来，都不该把原文带进帧里。
    """
    _patch_stream_boundaries(monkeypatch, fake_db)
    _patch_real_vision(monkeypatch)
    monkeypatch.setattr(vision_service, "_request_image_description", _async_boom)

    with caplog.at_level(logging.WARNING):
        body = _run_attachment_stream(fake_db)

    _assert_sse_frames_clean(body, "图片分析失败 SSE 帧")
    analysis = [
        json.loads(frame[len("data: "):])
        for frame in body.split("\n\n")
        if frame.strip().startswith("data: {")
    ]
    events = [event for event in analysis if event.get("type") != "trace"]
    error_event = next(event for event in events if event.get("type") == "error")
    assert error_event["message"].startswith(vision_service.IMAGE_ANALYSIS_FAILED_MESSAGE)
    # 原文仍可诊断：落日志，且与用户看到的编号对得上。
    error_id = _error_id_in(body)
    assert INTERNAL_ERROR_TEXT in caplog.text
    assert f"error_id={error_id}" in caplog.text


# --- 同一通路（LLM / 检索）里其余的 str(exc) 出口 ---------------------------
#
# 这几处不在 issue 点名的 6 处坐标内，但写的是同一条学习轨迹 / 同一个 SSE 流：
# `llm._trace_add()` 写的就是 `chat_service.stream_chat` 创建的那个 TraceRecorder，
# 失败事件随后编成 SSE 帧推给浏览器；`generation_failed` 事件与 error 事件的 message
# 更是被前端原样渲染（src/stores/chat.js 直接取 error.message）。
# CodeQL 告警 #4 的污点汇点正是 `chat_service.py` 的 `StreamingResponse`，只修 6 处坐标
# 不足以让该告警转为 fixed，所以这里按同一判据一并收敛。


def _collect_llm_events(*args, **kwargs):
    async def _run():
        return [event async for event in llm.stream_answer_events(*args, **kwargs)]

    return asyncio.run(_run())


def _failing_stream(*, fail_on):
    """_stream_model_chunks 替身：在第 fail_on 次调用（1=DeepSeek，2=后备）抛异常。"""
    calls = {"count": 0}

    async def fake_stream(model, messages):
        calls["count"] += 1
        if calls["count"] in fail_on:
            raise RuntimeError(INTERNAL_ERROR_TEXT)
        yield ""  # pragma: no cover - 本文件只走失败分支

    return fake_stream


def test_llm_generation_failure_trace_frame_hides_internal_error(monkeypatch, real_trace, caplog):
    """DeepSeek 生成失败：轨迹帧里只给固定文案，原文进日志。"""
    monkeypatch.setattr(llm, "_stream_model_chunks", _failing_stream(fail_on={1}))
    monkeypatch.setattr(llm, "DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "TEXT_FALLBACK_ENABLED", False)

    with caplog.at_level(logging.WARNING):
        _collect_llm_events("问题", "上下文", "", learning_trace.TraceRecorder(user_id=1), use_rag=True)

    result = _stage_result(real_trace.events, "langchain_generation_failed")
    _assert_no_leak(str(result.get("error")), "langchain_generation_failed result")
    assert result["error"] == llm.LLM_GENERATION_FAILED_MESSAGE
    assert INTERNAL_ERROR_TEXT in caplog.text


def test_llm_text_fallback_failure_hides_internal_error(monkeypatch, caplog):
    """后备模型也失败：error 事件的 message（前端原样显示）不得含异常原文。"""
    monkeypatch.setattr(llm, "_stream_model_chunks", _failing_stream(fail_on={1, 2}))
    monkeypatch.setattr(llm, "DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "TEXT_FALLBACK_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "TEXT_FALLBACK_ENABLED", True)

    with caplog.at_level(logging.WARNING):
        events = _collect_llm_events("问题", "上下文", "", None, use_rag=True)

    errors = [event for event in events if isinstance(event, dict) and event.get("type") == "error"]
    assert errors, f"后备模型失败时应产出 error 事件：{events}"
    for event in errors:
        _assert_no_leak(str(event.get("message")), "SSE error 事件 message")
    assert errors[0]["message"].startswith(llm.ANSWER_GENERATION_FAILED_MESSAGE)
    # 原文仍可诊断：落日志，且与用户看到的编号对得上。
    assert INTERNAL_ERROR_TEXT in caplog.text
    error_id = errors[0]["message"].split("错误编号：", 1)[1].rstrip("）")
    assert f"error_id={error_id}" in caplog.text


def test_retrieval_router_failure_reason_hides_internal_error(monkeypatch, caplog):
    """路由模型失败：reason 会进 SSE 轨迹帧与消息负载，不得含异常原文。"""
    import rag.retrieval as retrieval

    async def boom(_payload):
        raise RuntimeError(INTERNAL_ERROR_TEXT)

    monkeypatch.setattr(retrieval, "call_router_json", boom)

    with caplog.at_level(logging.WARNING):
        decision = asyncio.run(retrieval.decide_need_rag("问题"))

    _assert_no_leak(str(decision.get("reason")), "rag_gate.reason")
    assert INTERNAL_ERROR_TEXT in caplog.text


def test_retrieval_query_plan_failure_hides_internal_error(monkeypatch, caplog):
    """查询规划失败：query_plan 会进 SSE 轨迹帧与 retrieval_trace。"""
    import rag.retrieval as retrieval

    async def boom(*_args, **_kwargs):
        raise RuntimeError(INTERNAL_ERROR_TEXT)

    monkeypatch.setattr(retrieval, "call_chat_json", boom)

    with caplog.at_level(logging.WARNING):
        plan = asyncio.run(retrieval.build_query_plan("问题"))

    _assert_no_leak(str(plan.get("error")), "query_plan.error")
    assert plan["error"], "失败标记必须保留，调用方靠它区分规划是否成功"
    assert INTERNAL_ERROR_TEXT in caplog.text


# --- 检索轨迹：向量化后端不可达（retrieval_trace 的两个载体） ----------------
#
# `retrieval.py` 从 `asyncio.gather(..., return_exceptions=True)` 的**返回值**里取出
# `EmbeddingBackendError` 再 `str()` 化——它不在任何 `except` 体内，所以「逐个体检
# except 处理器」的扫法看不见这条通路，只能按「哪些数据最终进了响应体」回查。
# `retrieval_trace` 由 `chat_service` 用 `json.dumps` 落进 assistant 消息，随后
# `crud.chat.serialize_message` 把它原样放进 `GET /api/chat/conversations/{cid}`
# 的响应体；同一份轨迹里的 `embedding.last_error` 是 `embedding_backend_status()`
# 的进程级状态——**没赶上这次失败的用户**也会拿到它，因此两个载体都要断言。

EMBEDDING_UPSTREAM_BASE = "https://embedding.internal.example/v1"
EMBEDDING_TRANSPORT_ERROR = "[Errno 11001] getaddrinfo failed"
# 这些字样只要出现在用户侧负载里，就说明上游地址或原始异常回显了。
EMBEDDING_LEAK_MARKERS = ("embedding.internal.example", "/v1/embeddings", EMBEDDING_TRANSPORT_ERROR)


class _FailingEmbeddingTransport:
    """httpx.Client 替身：只替换传输层，抛真实的 `httpx.ConnectError`。

    与 `test_milvus_client.py` 的 `_FakeHttpClient(error=...)` 同一口径：被测的是
    「异常文本进不进用户负载」，不是 httpx 本身，因此边界止于传输层。
    """

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def post(self, url, json=None, headers=None):
        raise httpx.ConnectError(EMBEDDING_TRANSPORT_ERROR)


@pytest.fixture
def clean_embedding_state():
    """`_embedding_state` 是进程级全局：用例前后都清空，避免影响同进程里的其它用例。"""

    def reset():
        milvus_client._embedding_state.update(source="", last_error="", last_error_at="", last_used_at="")

    reset()
    yield
    reset()


def _configure_failing_embedding(monkeypatch) -> None:
    monkeypatch.setattr(milvus_client, "EMBEDDING_BASE_URL", EMBEDDING_UPSTREAM_BASE)
    monkeypatch.setattr(milvus_client, "EMBEDDING_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(
        milvus_client.httpx, "Client", lambda *_args, **_kwargs: _FailingEmbeddingTransport()
    )


def _fake_query_vectors(query, top_k, knowledge_base_id, route):
    """只替换 Milvus 连接与召回结果，向量化仍走真实实现。

    `milvus_client._embedding_fn` 是真实实例：真实 httpx 失败 → 真实
    `_embedding_failure()` 拼出含上游地址的文案 → 真实 `EmbeddingBackendError`。
    """
    milvus_client._embedding_fn([query])
    return []


def _failing_query_plan() -> dict:
    return {
        "original_question": "迟到30分钟以内怎么罚款",
        "simplified_question": "",
        "sub_questions": [],
        "rewrites": [],
        "keywords": ["考勤", "迟到"],
        "required_evidence": [],
    }


def test_retrieval_embedding_failure_trace_hides_internal_error(monkeypatch, clean_embedding_state, caplog):
    """向量化后端不可达：retrieval_trace 的两个载体都不得把上游地址与原始异常交给用户。"""
    import rag.retrieval as retrieval

    _configure_failing_embedding(monkeypatch)

    async def fake_rerank_chunks(question, chunks):
        return chunks, {"status": "done", "items": []}

    monkeypatch.setattr(retrieval, "query_vectors", _fake_query_vectors)
    monkeypatch.setattr(retrieval, "keyword_recall", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(retrieval, "rerank_chunks", fake_rerank_chunks)

    with caplog.at_level(logging.WARNING):
        _chunks, trace = asyncio.run(
            retrieval.retrieve_knowledge(
                "考勤 迟到 罚款", knowledge_base_id=1, db=object(), query_plan=_failing_query_plan()
            )
        )

    # 阳性对照：这段敏感文本确实产生过，否则下面的「负载干净」可能只是空跑。
    assert EMBEDDING_TRANSPORT_ERROR in milvus_client.embedding_backend_status()["last_error"]

    # 降级语义与失败标记不变：收敛的是文本，不是行为。
    assert trace["embedding_error"]
    assert trace["embedding"]["mode"] == "unavailable"

    # 1) 轨迹整体（embedding_error + embedding.last_error）不含上游地址/原文
    _assert_no_leak(json.dumps(trace, ensure_ascii=False), "retrieval_trace", EMBEDDING_LEAK_MARKERS)

    # 2) 可诊断性不降级：原文、真实栈与用户可报出的编号都能在服务端日志里对上
    assert EMBEDDING_TRANSPORT_ERROR in caplog.text
    # 本处不在 except 体内，`exc_info=True` 只会落出 "NoneType: None"；类名只可能来自
    # 真实 traceback，因此这一行锁住「异常对象自带 __traceback__ 被真的用上」。
    assert "EmbeddingBackendError" in caplog.text
    error_id = trace["embedding_error"].split("错误编号：", 1)[1].rstrip("）")
    assert f"error_id={error_id}" in caplog.text

    # 3) 真落库形态 → 真 serialize_message → 消息历史接口的用户可见负载
    message = Message(id=1, role="assistant", content="回答", conversation_id="c-1")
    message.retrieval_trace = json.dumps(trace, ensure_ascii=False)
    body = json.dumps(crud_chat.serialize_message(message), ensure_ascii=False, default=str)
    _assert_no_leak(body, "GET /api/chat/conversations/{cid} 响应体", EMBEDDING_LEAK_MARKERS)


def test_embedding_trace_status_masks_last_error_on_the_copy_only(clean_embedding_state):
    """轨迹副本收敛 `last_error`；`embedding_backend_status()` 的返回值是诊断面，保持不变。"""
    error = milvus_client._embedding_failure(
        f"向量化接口调用失败（{EMBEDDING_UPSTREAM_BASE}/embeddings）：{EMBEDDING_TRANSPORT_ERROR}"
    )
    assert isinstance(error, milvus_client.EmbeddingBackendError)

    status = milvus_client.embedding_backend_status()
    assert EMBEDDING_TRANSPORT_ERROR in status["last_error"]

    masked = milvus_client.embedding_trace_status(status)
    _assert_no_leak(json.dumps(masked, ensure_ascii=False), "轨迹副本", EMBEDDING_LEAK_MARKERS)
    assert masked["mode"] == "unavailable", "降级语义必须保留，前端靠它区分是否可用"
    # 收敛的是副本：状态接口的原文没有被就地改写（既有用例锁定着它）。
    assert EMBEDDING_TRANSPORT_ERROR in status["last_error"]


def test_direct_mode_trace_embedding_status_hides_internal_error(
    monkeypatch, fake_db, real_trace, clean_embedding_state
):
    """直答模式（不跑检索）同样会把这份状态写进 retrieval_trace 并落库回查。"""
    _patch_stream_boundaries(monkeypatch, fake_db)

    async def fake_decide_need_rag(*_args, **_kwargs):
        return {"need_rag": False, "route": "direct", "confidence": 1.0, "source": "test", "reason": "直答"}

    monkeypatch.setattr(chat_service, "decide_need_rag", fake_decide_need_rag)
    milvus_client._embedding_failure(
        f"向量化接口调用失败（{EMBEDDING_UPSTREAM_BASE}/embeddings）：{EMBEDDING_TRANSPORT_ERROR}"
    )

    _run_stream(fake_db)

    assistants = fake_db.added_by_role("assistant")
    assert assistants, "直答模式也应把 assistant 消息落库"
    persisted = str(assistants[0].retrieval_trace)
    _assert_no_leak(persisted, "assistant 消息的 retrieval_trace", EMBEDDING_LEAK_MARKERS)
    assert json.loads(persisted)["embedding"]["mode"] == "unavailable"


def test_memory_summary_update_failure_trace_hides_internal_error(monkeypatch, caplog):
    """滑出窗口记忆更新失败：落库的轨迹事件会被回查给用户，不得含异常原文。"""
    import rag.memory_service as memory_service

    captured = []

    class _BoomDb:
        def query(self, *_args, **_kwargs):
            raise RuntimeError(INTERNAL_ERROR_TEXT)

        def close(self):
            pass

    monkeypatch.setattr(memory_service, "SessionLocal", lambda: _BoomDb())
    monkeypatch.setattr(
        memory_service,
        "append_trace_event",
        lambda trace_id, stage, function, **kwargs: captured.append((stage, kwargs)),
    )

    with caplog.at_level(logging.WARNING):
        asyncio.run(memory_service._update_memory_summary_from_sliding_window("conv-1", "trace-1"))

    assert captured, "更新失败时必须留下轨迹事件"
    stage, kwargs = captured[0]
    assert stage == "memory_summary_update_failed"
    _assert_no_leak(str(kwargs.get("result")), "memory_summary_update_failed result")
    assert kwargs["result"]["error"] == memory_service.MEMORY_SUMMARY_UPDATE_FAILED_MESSAGE
    assert INTERNAL_ERROR_TEXT in caplog.text


# --- RAGAS 后台评测：Message.ragas_error（前端原样渲染） ---------------------
#
# `_friendly_error` 的结果经 `_mark_message` 写进 `Message.ragas_error`，由
# `src/views/Chat.vue:157` 原样渲染，同时进 `ragas_*` 轨迹事件供用户回查——与 SSE 帧、
# HTTP detail 是同一个判据下的用户可见面。它此前有四条返回路径直接拼 `str(exc)`。


def test_friendly_error_never_embeds_exception_text(caplog):
    """四条返回路径都不得把异常原文拼进返回值；原文只落日志。"""
    import rag.ragas_eval as ragas_eval

    cases = [
        RuntimeError(INTERNAL_ERROR_TEXT),
        PermissionError(13, "Permission denied", "/srv/app/uploads/考勤.pdf"),
        RuntimeError(f"Embedding 调用失败: {INTERNAL_ERROR_TEXT}"),
        RuntimeError(f"Connection error: {INTERNAL_ERROR_TEXT}"),
    ]

    with caplog.at_level(logging.WARNING):
        rendered = [ragas_eval._friendly_error(exc) for exc in cases]

    for exc, text in zip(cases, rendered):
        # 原文与文件路径（issue 点名的一类文本）都不得出现在返回值里。
        _assert_no_leak(text, f"_friendly_error({type(exc).__name__})", markers=LEAK_MARKERS + ("/srv/app",))

    assert INTERNAL_ERROR_TEXT in caplog.text
    assert "/srv/app/uploads/考勤.pdf" in caplog.text
    # 分类能力保留：可读的原因仍在，只是不再附原文。
    assert rendered[2].startswith("Embedding 调用失败")
    assert rendered[3].startswith("DeepSeek 调用失败")


class _FakeMessage:
    ragas_status = ""
    ragas_scores = ""
    ragas_error = ""


class _FakeMessageDb:
    """`_mark_message` 的最小替身：让 message 可被取出并记录 commit。"""

    def __init__(self, message):
        self.message = message
        self.committed = False
        self.closed = False

    def query(self, _model):
        return self

    def filter_by(self, **_kwargs):
        return self

    def first(self):
        return self.message

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def test_ragas_evaluation_failure_hides_internal_error(monkeypatch, caplog):
    """RAGAS 后台评测异常：写回消息与轨迹事件的文案不得含异常原文。"""
    import rag.ragas_eval as ragas_eval

    message = _FakeMessage()
    db = _FakeMessageDb(message)
    captured = []

    monkeypatch.setattr(ragas_eval, "RAGAS_ENABLED", True)
    monkeypatch.setattr(ragas_eval, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        ragas_eval,
        "append_trace_event",
        lambda trace_id, stage, function, **kwargs: captured.append((stage, kwargs)),
    )
    monkeypatch.setattr(ragas_eval, "_evaluate_message_sync", _boom)

    with caplog.at_level(logging.WARNING):
        asyncio.run(ragas_eval.evaluate_message_async(1, "问题", "回答", ["上下文"], "trace-1"))

    _assert_no_leak(message.ragas_error, "message.ragas_error")
    assert message.ragas_error.startswith("RAGAS 评测失败：")
    assert db.committed and db.closed

    failed = [kwargs for stage, kwargs in captured if stage == "ragas_failed"]
    assert failed, f"评测失败时必须留下轨迹事件：{[stage for stage, _ in captured]}"
    _assert_no_leak(str(failed[0].get("result")), "ragas_failed result")
    # 消息与轨迹事件必须带同一个编号，否则用户报出来的编号在日志里对不上。
    error_id = message.ragas_error.split("错误编号：", 1)[1].rstrip("）")
    assert failed[0]["result"]["error"] == message.ragas_error.split("：", 1)[1]

    assert INTERNAL_ERROR_TEXT in caplog.text
    assert f"error_id={error_id}" in caplog.text


def test_ragas_metric_failure_hides_internal_error(caplog):
    """单个指标失败：`_format_metric_errors` 拼出的那串就是写进 ragas_error 的文本。"""
    import rag.ragas_eval as ragas_eval

    with caplog.at_level(logging.WARNING):
        error_text = ragas_eval._format_metric_errors({
            "faithfulness": ragas_eval._friendly_error(RuntimeError(INTERNAL_ERROR_TEXT)),
            "response_relevancy": ragas_eval._friendly_error(
                RuntimeError(f"Embedding 调用失败: {INTERNAL_ERROR_TEXT}")
            ),
        })

    _assert_no_leak(error_text, "_format_metric_errors")
    assert error_text.startswith("部分 RAGAS 指标评测失败：faithfulness: ")
    assert "response_relevancy: Embedding 调用失败" in error_text
    assert INTERNAL_ERROR_TEXT in caplog.text
