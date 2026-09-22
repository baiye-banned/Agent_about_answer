"""补齐此前零测试引用的边界模块（issue #18 D）：

- `service/trace_service.py`：SSE 组帧与「安全轨迹」包装器
- `rag/chains.py`：RAG 生成入口的参数透传与惰性
- `service/oss_service.py`：OSS 主机/签名/URL/PUT 请求
- `rag/vision_service.py`：有效问题构造、图片 URL、描述分类、视觉请求

全部断言被测函数自身的输出与调用参数（URL、签名、请求头、异常类型），不依赖网络。
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from email.utils import formatdate
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException

from rag import chains, vision_service
from service import oss_service, trace_service


OSS_CONFIG = {
    "OSS_ACCESS_KEY_ID": "test-id",
    "OSS_ACCESS_KEY_SECRET": "test-secret",
    "OSS_BUCKET": "demo",
    "OSS_ENDPOINT": "https://oss-cn-hangzhou.aliyuncs.com",
}


# 铸造形态的对象键字面量，照抄上传路径实际铸出来的样子。issue #181 之后形态判据也管着
# URL 构造口（`_public_oss_url`），凡是要真的构造出 OSS URL 的用例都得用这个形状；
# 非自铸形状的表现由 tests/test_vision_outbound_guard_181.py 覆盖。
MINTED_KEY = "rag-chat/2026/09/21/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png"


def _patch_oss_config(monkeypatch, **overrides):
    for name, value in {**OSS_CONFIG, **overrides}.items():
        monkeypatch.setattr(oss_service, name, value)


# ---------------------------------------------------------------------------
# service/trace_service.py
# ---------------------------------------------------------------------------

class _RecordingTrace:
    def __init__(self, payloads=None, error=None):
        self.payloads = payloads or []
        self.error = error
        self.calls = []

    def drain_sse_payloads(self):
        if self.error is not None:
            raise self.error
        return list(self.payloads)

    def add(self, *args, **kwargs):
        if self.error is not None:
            raise self.error
        self.calls.append(("add", args, kwargs))
        return {"id": len(self.calls)}

    def finish(self, *args, **kwargs):
        if self.error is not None:
            raise self.error
        self.calls.append(("finish", args, kwargs))

    def attach(self, *args, **kwargs):
        if self.error is not None:
            raise self.error
        self.calls.append(("attach", args, kwargs))


def test_trace_sse_payloads_frames_each_drained_event():
    trace = _RecordingTrace(payloads=[
        {"type": "trace", "trace_id": "t1", "event": {"stage": "request_received"}},
        {"type": "trace", "trace_id": "t1", "event": {"stage": "retrieval_completed", "note": "检索完成"}},
    ])

    payloads = trace_service._trace_sse_payloads(trace)

    assert len(payloads) == 2
    assert payloads[0] == 'data: {"type": "trace", "trace_id": "t1", "event": {"stage": "request_received"}}\n\n'
    # ensure_ascii=False：中文必须原样下发，前端不需要再解转义
    assert "检索完成" in payloads[1]
    assert "\\u" not in payloads[1]
    assert payloads[1].endswith("\n\n")
    assert json.loads(payloads[1][len("data: "):-2]) == {
        "type": "trace",
        "trace_id": "t1",
        "event": {"stage": "retrieval_completed", "note": "检索完成"},
    }


def test_trace_sse_payloads_is_empty_when_nothing_drained():
    assert trace_service._trace_sse_payloads(_RecordingTrace(payloads=[])) == []


def test_safe_trace_wrappers_forward_calls_and_return_values():
    trace = _RecordingTrace()

    assert trace_service._safe_trace_add(trace, "stage", "function", note="n") == {"id": 1}
    trace_service._safe_trace_attach(trace, conversation_id=5)
    trace_service._safe_trace_finish(trace, "done", message_id=9)

    assert trace.calls == [
        ("add", ("stage", "function"), {"note": "n"}),
        ("attach", (), {"conversation_id": 5}),
        ("finish", ("done",), {"message_id": 9}),
    ]


def test_safe_trace_wrappers_swallow_recorder_failures():
    trace = _RecordingTrace(error=RuntimeError("trace backend down"))

    # 轨迹失败不能影响主链路：包装器必须吞掉异常并给出兜底返回值
    assert trace_service._safe_trace_add(trace, "stage", "function") == {}
    assert trace_service._safe_trace_attach(trace, conversation_id=1) is None
    assert trace_service._safe_trace_finish(trace, "done") is None


# ---------------------------------------------------------------------------
# rag/chains.py
# ---------------------------------------------------------------------------

def _collect_events(iterator):
    async def _run():
        return [event async for event in iterator]

    return asyncio.run(_run())


def test_stream_rag_answer_forwards_arguments_and_preserves_event_order(monkeypatch):
    calls = []
    sentinel = object()
    events = [{"content": "甲"}, {"type": "reset", "reason": "text_fallback"}, "乙"]

    async def fake_stream_answer_events(question, context, memory_context, trace, use_rag):
        calls.append({
            "question": question,
            "context": context,
            "memory_context": memory_context,
            "trace": trace,
            "use_rag": use_rag,
        })
        for event in events:
            yield event

    monkeypatch.setattr(chains, "stream_answer_events", fake_stream_answer_events)

    collected = _collect_events(
        chains.stream_rag_answer("问题", "上下文", memory_context="记忆", trace=sentinel, use_rag=False)
    )

    assert calls == [{
        "question": "问题",
        "context": "上下文",
        "memory_context": "记忆",
        "trace": sentinel,
        "use_rag": False,
    }]
    assert collected == events


def test_stream_rag_answer_defaults_and_laziness(monkeypatch):
    calls = []

    async def fake_stream_answer_events(question, context, memory_context, trace, use_rag):
        calls.append((question, context, memory_context, trace, use_rag))
        yield "片段"

    monkeypatch.setattr(chains, "stream_answer_events", fake_stream_answer_events)

    stream = chains.stream_rag_answer("问题", "上下文")
    # 未开始迭代前不得触发下游（否则首字延迟会变成阻塞调用）
    assert calls == []

    assert _collect_events(stream) == ["片段"]
    assert calls == [("问题", "上下文", "", None, True)]


# ---------------------------------------------------------------------------
# service/oss_service.py
# ---------------------------------------------------------------------------

def test_oss_host_prefixes_bucket_unless_already_present(monkeypatch):
    _patch_oss_config(monkeypatch)
    assert oss_service._oss_host() == "demo.oss-cn-hangzhou.aliyuncs.com"

    _patch_oss_config(monkeypatch, OSS_ENDPOINT="http://demo.oss-cn-hangzhou.aliyuncs.com/")
    assert oss_service._oss_host() == "demo.oss-cn-hangzhou.aliyuncs.com"


def test_oss_object_path_percent_encodes_but_keeps_slashes():
    assert oss_service._oss_object_path("uploads/文件 1.png") == "/uploads/%E6%96%87%E4%BB%B6%201.png"


def test_public_oss_url_requires_complete_config(monkeypatch):
    _patch_oss_config(monkeypatch, OSS_BUCKET="")
    with pytest.raises(HTTPException) as excinfo:
        oss_service._public_oss_url(MINTED_KEY)
    assert (excinfo.value.status_code, excinfo.value.detail) == (500, "OSS 环境变量未完整配置")


def test_public_oss_url_builds_host_and_path(monkeypatch):
    _patch_oss_config(monkeypatch)
    assert oss_service._public_oss_url(MINTED_KEY) == f"https://demo.oss-cn-hangzhou.aliyuncs.com/{MINTED_KEY}"


def test_sign_oss_url_signs_the_documented_string(monkeypatch):
    _patch_oss_config(monkeypatch)

    url = oss_service._sign_oss_url("uploads/文件.png", expires=600)

    parsed = urlsplit(url)
    params = parse_qs(parsed.query)
    assert parsed.netloc == "demo.oss-cn-hangzhou.aliyuncs.com"
    assert parsed.path == "/uploads/%E6%96%87%E4%BB%B6.png"
    assert params["OSSAccessKeyId"] == ["test-id"]

    expires_at = int(params["Expires"][0])
    assert abs(expires_at - (int(time.time()) + 600)) <= 5
    expected = base64.b64encode(
        hmac.new(
            b"test-secret",
            f"GET\n\n\n{expires_at}\n/demo/uploads/文件.png".encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("utf-8")
    assert params["Signature"] == [expected]


class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class _FakeAsyncClient:
    def __init__(self, response, **_kwargs):
        self.response = response
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return False

    async def put(self, url, content=None, headers=None):
        self.requests.append({"url": url, "content": content, "headers": headers})
        return self.response

    async def post(self, url, json=None, headers=None):
        self.requests.append({"url": url, "json": json, "headers": headers})
        return self.response


def _patch_http_client(monkeypatch, response):
    client = _FakeAsyncClient(response)

    def factory(**kwargs):
        client.kwargs = kwargs
        return client

    monkeypatch.setattr(oss_service.httpx, "AsyncClient", factory)
    return client


def test_put_oss_object_sends_signed_public_read_request(monkeypatch):
    _patch_oss_config(monkeypatch)
    client = _patch_http_client(monkeypatch, _FakeResponse(200))

    asyncio.run(oss_service._put_oss_object("uploads/a.png", b"payload", "image/png"))

    assert len(client.requests) == 1
    request = client.requests[0]
    assert request["url"] == "https://demo.oss-cn-hangzhou.aliyuncs.com/uploads/a.png"
    assert request["content"] == b"payload"
    assert request["headers"]["Content-Type"] == "image/png"
    assert request["headers"]["x-oss-object-acl"] == "public-read"
    assert request["headers"]["Host"] == "demo.oss-cn-hangzhou.aliyuncs.com"
    assert request["headers"]["Authorization"].startswith("OSS test-id:")

    # Authorization 里的签名必须由真实 string-to-sign 推出（PUT + acl + 资源路径）
    date = request["headers"]["Date"]
    expected = base64.b64encode(
        hmac.new(
            b"test-secret",
            f"PUT\n\nimage/png\n{date}\nx-oss-object-acl:public-read\n/demo/uploads/a.png".encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("utf-8")
    assert request["headers"]["Authorization"] == f"OSS test-id:{expected}"
    assert date == formatdate(usegmt=True)


def test_put_oss_object_raises_on_error_status(monkeypatch):
    _patch_oss_config(monkeypatch)
    _patch_http_client(monkeypatch, _FakeResponse(403, "AccessDenied" * 40))

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(oss_service._put_oss_object("uploads/a.png", b"payload", "image/png"))

    message = str(excinfo.value)
    assert message.startswith("403 ")
    assert len(message) <= len("403 ") + 200


# ---------------------------------------------------------------------------
# rag/vision_service.py
# ---------------------------------------------------------------------------

def test_build_effective_question_trims_and_skips_analysis_without_attachments():
    assert asyncio.run(vision_service._build_effective_question("  迟到怎么处理  ")) == ("迟到怎么处理", {})
    assert asyncio.run(vision_service._build_effective_question("问题", [])) == ("问题", {})


def test_build_image_urls_skips_attachments_without_object_key(monkeypatch):
    # 键用铸造形态：这里断的是「没有键的条目被跳过」，不是「任意键都被接受」。
    # 百分号编码本身另由 `test_oss_object_path_percent_encodes_but_keeps_slashes` 覆盖。
    _patch_oss_config(monkeypatch)

    urls = vision_service._build_image_urls([
        {"object_key": MINTED_KEY},
        {"object_key": ""},
        {"file_name": "无 key.png"},
    ])

    assert urls == [f"https://demo.oss-cn-hangzhou.aliyuncs.com/{MINTED_KEY}"]


def test_build_effective_question_reports_failure_when_vision_not_configured(monkeypatch):
    _patch_oss_config(monkeypatch)
    monkeypatch.setattr(vision_service, "VISION_API_KEY", "")

    question, analysis = asyncio.run(
        vision_service._build_effective_question("这是什么", [{"object_key": MINTED_KEY}])
    )

    assert question == "这是什么"
    assert analysis["status"] == "failed"
    assert analysis["description"] == ""
    assert analysis["error"] == "图片内容提取失败，请检查 VISION_MODEL/VISION_API_KEY/OSS URL。"


def test_build_effective_question_reports_missing_object_key(monkeypatch):
    monkeypatch.setattr(vision_service, "VISION_API_KEY", "sk-vision")

    question, analysis = asyncio.run(vision_service._build_effective_question("这是什么", [{"file_name": "a.png"}]))

    assert question == "这是什么"
    assert analysis["status"] == "failed"
    assert analysis["error"] == "图片附件缺少可访问的 OSS object_key，请重新上传后再试。"


def test_image_analysis_prompts_embed_the_user_question():
    with_question = vision_service._image_analysis_prompts("第三题选什么")
    without_question = vision_service._image_analysis_prompts("   ")

    assert len(with_question) == len(without_question) == 2
    assert all("用户问题：第三题选什么" in prompt for prompt in with_question)
    assert all("用户问题" not in prompt for prompt in without_question)


def test_classify_image_analysis_maps_phrases_to_status():
    assert vision_service._classify_image_analysis("图中显示：1+1=2，选项A") == ("success", "")
    assert vision_service._classify_image_analysis("") == (
        "failed",
        "图片内容提取失败：视觉模型未返回图片描述。",
    )
    assert vision_service._classify_image_analysis("图片看不清") == (
        "failed",
        "图片内容未能清晰识别，请检查图片清晰度后重试。",
    )
    # 既报「看不清」又给出可用内容 → 部分识别
    assert vision_service._classify_image_analysis("部分区域看不清，可见文字为：迟到罚款50元") == (
        "partial",
        "图片内容仅部分识别，请结合文字问题查看。",
    )
    assert vision_service._classify_image_analysis("看起来大致是道数学题") == (
        "partial",
        "图片内容仅部分识别，请结合文字问题查看。",
    )


def test_request_image_description_builds_payload_and_strips_content(monkeypatch):
    monkeypatch.setattr(vision_service, "VISION_API_KEY", "sk-vision")
    monkeypatch.setattr(vision_service, "VISION_MODEL", "vision-model")
    monkeypatch.setattr(vision_service, "VISION_BASE_URL", "https://vision.example.com/v1")

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "  图片里是一张考勤表  "}}]}

    client = _FakeAsyncClient(_Response())

    def factory(**kwargs):
        client.kwargs = kwargs
        return client

    monkeypatch.setattr(vision_service.httpx, "AsyncClient", factory)

    description = asyncio.run(vision_service._request_image_description("描述这张图", ["https://oss/a.png"]))

    assert description == "图片里是一张考勤表"
    assert client.requests[0]["url"] == "https://vision.example.com/v1/chat/completions"
    assert client.requests[0]["headers"]["Authorization"] == "Bearer sk-vision"
    payload = client.requests[0]["json"]
    assert payload["model"] == "vision-model"
    assert payload["messages"][1]["content"][0] == {"type": "text", "text": "描述这张图"}
    assert payload["messages"][1]["content"][1] == {"type": "image_url", "image_url": {"url": "https://oss/a.png"}}


def test_request_image_description_raises_on_error_status(monkeypatch):
    monkeypatch.setattr(vision_service, "VISION_API_KEY", "sk-vision")

    class _Response:
        status_code = 429
        text = "rate limited"

    client = _FakeAsyncClient(_Response())
    monkeypatch.setattr(vision_service.httpx, "AsyncClient", lambda **kwargs: client)

    with pytest.raises(RuntimeError, match="图片内容提取失败"):
        asyncio.run(vision_service._request_image_description("描述这张图", ["https://oss/a.png"]))
