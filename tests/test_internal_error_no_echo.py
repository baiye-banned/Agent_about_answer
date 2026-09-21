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

import docx
import pypdf
import pytest
from fastapi import HTTPException

import rag.learning_trace as learning_trace
import rag.llm as llm
from conftest import FakeKnowledgeBase
from crud import knowledge_file
from rag import vision_service
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
IMAGE_ATTACHMENT = {"object_key": "uploads/考勤.png", "file_name": "考勤.png", "content_type": "image/png"}


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
