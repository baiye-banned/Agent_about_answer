"""回归（issue #259）：模型正常结束但零正文，必须显式失败，不能只发 [DONE]。

主路径只替换模型工厂，真实执行 chat_service -> rag.chains -> rag.llm，包括空 chunk
过滤与后备模型 reset；数据库和 trace 使用 conftest 的共享替身。直接异常/图片早退
用例另替换各自边界，证明新分支不把异常、取消和生成前返回误归类为 empty_response。
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import rag.llm as llm
from conftest import (
    FakeKnowledgeBase,
    FakeQuery,
    collect_stream,
    frames_of_type,
    parse_sse_frames,
    streamed_content,
)
from model.models import Conversation, User
from schema.schemas import ChatRequest
from service import chat_service, rate_limit


EMPTY_MESSAGE = "本次未生成回答，请重新发送"


class FakeModel:
    """仅模拟 provider 的 astream：空 content 不等于异常，正常穷尽也不保证有正文。"""

    def __init__(self, contents=(), failure=None):
        self.contents = contents
        self.failure = failure

    async def astream(self, messages):
        for content in self.contents:
            yield SimpleNamespace(content=content)
        if self.failure is not None:
            raise self.failure


@pytest.fixture
def stream_case(monkeypatch, fake_db, trace_recorder_cls):
    """独立的容量 1 闸门；第二问复用 FakeDb 已记录的同一条会话。"""
    original_query = fake_db.query

    def query(model):
        if model is Conversation:
            return FakeQuery([row for row in fake_db.added if isinstance(row, Conversation)])
        return original_query(model)

    monkeypatch.setattr(fake_db, "query", query)
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: fake_db)
    monkeypatch.setattr(
        chat_service, "authenticate", lambda db, authorization: db.query(User).first()
    )
    monkeypatch.setattr(chat_service, "TraceRecorder", trace_recorder_cls)
    monkeypatch.setattr(
        chat_service, "resolve_knowledge_base", lambda db, kid, user_id: FakeKnowledgeBase()
    )

    async def effective_question(question, attachments):
        return question, {"status": "skipped"}

    async def recent_memory(*_args, **_kwargs):
        return ""

    async def direct_gate(*_args, **_kwargs):
        return {"need_rag": False, "route": "direct", "reason": "offline test"}

    monkeypatch.setattr(chat_service, "_build_effective_question", effective_question)
    monkeypatch.setattr(chat_service, "_build_recent_memory_text", recent_memory)
    monkeypatch.setattr(chat_service, "decide_need_rag", direct_gate)
    monkeypatch.setattr(chat_service, "_schedule_memory_summary_update", lambda *_args: None)
    monkeypatch.setattr(chat_service, "schedule_ragas_evaluation", lambda *_args: None)
    gate = rate_limit.ConcurrencyGate(1)
    monkeypatch.setattr(rate_limit, "chat_stream_slots", gate)
    monkeypatch.setattr(llm, "TEXT_FALLBACK_ENABLED", False)

    models = []
    calls = []

    def model_factory(**kwargs):
        assert kwargs["streaming"] is True
        model = models.pop(0)
        calls.append(model)
        return model

    monkeypatch.setattr(llm, "get_deepseek_model", model_factory)
    monkeypatch.setattr(llm, "get_text_fallback_model", model_factory)
    return SimpleNamespace(
        db=fake_db, traces=trace_recorder_cls.instances, gate=gate, models=models, calls=calls
    )


def _response(question="解释一下 RAG", conversation_id=None, attachments=None):
    return asyncio.run(chat_service.stream_chat(
        ChatRequest(question=question, conversation_id=conversation_id, attachments=attachments or []),
        authorization="Bearer synthetic-token",
    ))


def _assert_empty_failure(case, body):
    frames = parse_sse_frames(body)
    assert frames_of_type(frames, "error") == [{"type": "error", "message": EMPTY_MESSAGE}]
    assert body.endswith("data: [DONE]\n\n")
    assert streamed_content(frames) == ""
    assert case.db.added_by_role("assistant") == []
    assert len(case.db.added_by_role("user")) == 1
    trace = case.traces[-1]
    assert trace.status == "failed"
    assert trace.event("generation_failed")["result"] == {
        "reason": "empty_response", "message": EMPTY_MESSAGE
    }
    assert trace.event("assistant_not_saved")["result"]["saved"] is False
    assert trace.stages().count("generation_failed") == 1
    error_index = next(i for i, frame in enumerate(frames) if frame.get("type") == "error")
    before_error = [
        frame["event"]["stage"] for frame in frames[:error_index] if frame.get("type") == "trace"
    ]
    assert "generation_failed" in before_error
    assert "assistant_not_saved" in before_error
    assert "assistant_message_saved" not in trace.stages()
    assert "memory_summary_update_scheduled" not in trace.stages()
    assert case.gate.in_flight == 0


@pytest.mark.parametrize("contents", [[], ["", None, []], ["", [{"text": ""}], ""]])
def test_normal_completion_without_text_reports_error(stream_case, contents):
    stream_case.models.append(FakeModel(contents))
    body = collect_stream(_response().body_iterator)
    _assert_empty_failure(stream_case, body)
    assert len(stream_case.calls) == 1, "空回复处理不能暗中增加模型调用"


def test_empty_chunks_before_text_are_not_a_failure(stream_case):
    stream_case.models.append(FakeModel(["", None, "正常", "回答"]))
    body = collect_stream(_response().body_iterator)
    frames = parse_sse_frames(body)
    assert frames_of_type(frames, "error") == []
    assert streamed_content(frames) == "正常回答"
    assert stream_case.db.added_by_role("assistant")[0].content == "正常回答"
    assert stream_case.traces[-1].status == "done"
    assert "generation_failed" not in stream_case.traces[-1].stages()
    assert len(stream_case.calls) == 1
    assert stream_case.gate.in_flight == 0


def test_existing_model_error_is_not_duplicated(stream_case):
    stream_case.models.append(FakeModel(failure=RuntimeError("synthetic provider failure")))
    body = collect_stream(_response().body_iterator)
    errors = frames_of_type(parse_sse_frames(body), "error")
    assert len(errors) == 1
    assert errors[0]["message"] != EMPTY_MESSAGE
    trace = stream_case.traces[-1]
    assert trace.status == "failed"
    assert trace.stages().count("generation_failed") == 1
    assert trace.event("generation_failed")["result"].get("reason") != "empty_response"
    assert stream_case.db.added_by_role("assistant") == []
    assert stream_case.gate.in_flight == 0


@pytest.mark.parametrize("fallback_contents", [[], ["后备", "完整回答"]])
def test_reset_checks_only_the_final_model_text(monkeypatch, stream_case, fallback_contents):
    monkeypatch.setattr(llm, "TEXT_FALLBACK_ENABLED", True)
    monkeypatch.setattr(llm, "TEXT_FALLBACK_API_KEY", "offline-provider-fixture")
    stream_case.models.extend([
        FakeModel(["应被作废的半截回答"], RuntimeError("synthetic disconnect")),
        FakeModel(fallback_contents),
    ])
    body = collect_stream(_response().body_iterator)
    frames = parse_sse_frames(body)
    assert len(frames_of_type(frames, "reset")) == 1
    assert len(stream_case.calls) == 2
    if not fallback_contents:
        _assert_empty_failure(stream_case, body)
    else:
        assert frames_of_type(frames, "error") == []
        assert streamed_content(frames) == "后备完整回答"
        assert stream_case.db.added_by_role("assistant")[0].content == "后备完整回答"
        assert stream_case.traces[-1].status == "done"
        assert "generation_failed" not in stream_case.traces[-1].stages()
        assert stream_case.gate.in_flight == 0


def test_failed_trace_is_finished_before_error_can_disconnect(stream_case):
    stream_case.models.append(FakeModel())

    async def scenario():
        response = await chat_service.stream_chat(ChatRequest(question="问题"), "Bearer synthetic-token")
        iterator = response.body_iterator
        async for chunk in iterator:
            if frames_of_type(parse_sse_frames(chunk), "error"):
                assert stream_case.traces[-1].status == "failed"
                assert "assistant_not_saved" in stream_case.traces[-1].stages()
                # 此刻还没有消费 DONE；客户端错误处理立刻断开也必须留下明确终态。
                await iterator.aclose()
                return
        pytest.fail("没有收到空回复错误帧")

    asyncio.run(scenario())
    assert stream_case.gate.in_flight == 0


def test_cancelled_model_is_not_an_empty_completion(stream_case):
    stream_case.models.append(FakeModel(failure=asyncio.CancelledError()))
    response = _response()
    with pytest.raises(asyncio.CancelledError):
        collect_stream(response.body_iterator)
    trace = stream_case.traces[-1]
    assert trace.status == "failed"
    assert "generation_failed" not in trace.stages()
    assert "assistant_not_saved" not in trace.stages()
    assert stream_case.db.added_by_role("assistant") == []
    assert stream_case.gate.in_flight == 0


def test_direct_generator_exception_keeps_existing_semantics(monkeypatch, stream_case):
    async def broken_chain(*_args, **_kwargs):
        raise RuntimeError("synthetic chain failure")
        yield  # 让替身保留异步生成器接口，而不是 coroutine。

    monkeypatch.setattr(chat_service, "stream_rag_answer", broken_chain)
    response = _response()
    with pytest.raises(RuntimeError, match="synthetic chain failure"):
        collect_stream(response.body_iterator)
    trace = stream_case.traces[-1]
    assert trace.status == "failed"
    assert "generation_failed" not in trace.stages()
    assert "assistant_not_saved" not in trace.stages()
    assert stream_case.db.added_by_role("assistant") == []
    assert stream_case.gate.in_flight == 0


def test_image_failure_before_generation_is_not_reclassified(monkeypatch, stream_case):
    async def failed_image(*_args, **_kwargs):
        return "", {"status": "failed", "error": "图片识别失败（测试）"}

    monkeypatch.setattr(chat_service, "_build_effective_question", failed_image)
    response = _response(question="", attachments=[{"image_url": "synthetic-image"}])
    body = collect_stream(response.body_iterator)
    assert frames_of_type(parse_sse_frames(body), "error") == [
        {"type": "error", "message": "图片识别失败（测试）"}
    ]
    assert "generation_started" not in stream_case.traces[-1].stages()
    assert "generation_failed" not in stream_case.traces[-1].stages()
    assert stream_case.calls == []
    assert stream_case.db.added_by_role("user") == []
    assert stream_case.gate.in_flight == 0


def test_empty_request_is_rejected_before_generation(stream_case):
    with pytest.raises(HTTPException) as exc:
        _response(question=" ")
    assert exc.value.status_code == 400
    assert "generation_started" not in stream_case.traces[-1].stages()
    assert "generation_failed" not in stream_case.traces[-1].stages()
    assert stream_case.calls == []
    assert stream_case.gate.in_flight == 0


def test_next_question_is_accepted_after_empty_response(stream_case):
    stream_case.models.extend([FakeModel(), FakeModel(["第二问正常回答"])])
    first_body = collect_stream(_response().body_iterator)
    _assert_empty_failure(stream_case, first_body)
    conversation = frames_of_type(parse_sse_frames(first_body), "conversation")[0]["conversation"]
    second_body = collect_stream(_response(question="第二问", conversation_id=conversation["id"]).body_iterator)
    frames = parse_sse_frames(second_body)
    assert frames_of_type(frames, "error") == []
    assert frames_of_type(frames, "conversation")[0]["conversation"]["id"] == conversation["id"]
    assert streamed_content(frames) == "第二问正常回答"
    assert len(stream_case.db.added_by_role("user")) == 2
    assert [row.content for row in stream_case.db.added_by_role("assistant")] == ["第二问正常回答"]
    assert stream_case.traces[-1].status == "done"
    assert len(stream_case.calls) == 2
    assert stream_case.gate.in_flight == 0
