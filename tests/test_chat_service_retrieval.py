"""主链路测试：检索结果 → 上下文 → 流式回答 → 落库（issue #18）。

原用例把 18 个协作者全部换成替身，最后只断言替身自己写死的 `"回答"` 出现在 SSE 文本里：
即使实现不再拼接上下文、不再生成 sources、不再落库，用例依然是绿的。
现在只打桩真正的边界（鉴权、数据库、知识库解析、检索、模型生成），
记忆拼接、上下文拼装、来源生成、SSE 组帧、证据校验、消息落库全部跑真实实现，
并断言被测代码的真实行为：调用参数、状态变化、输出内容。
"""

import asyncio
import json
from types import SimpleNamespace

import rag.llm as llm
from conftest import (
    FakeKnowledgeBase,
    collect_stream,
    frames_of_type,
    parse_sse_frames,
    streamed_content,
)
from model.models import Conversation
from schema.schemas import ChatRequest
from service import chat_service
from service.utils_service import _build_sources


CHUNK_CONTENT = "员工迟到30分钟以内罚款50元，迟到超过30分钟按旷工处理。"
ANSWER_PREFIX = "根据知识库："
ANSWER_TEXT = ANSWER_PREFIX + CHUNK_CONTENT
CHUNK = {"file_name": "制度.txt", "content": CHUNK_CONTENT, "file_id": 1, "chunk_id": "a"}

RECENT_MEMORY_TEXT = "用户：迟到怎么处理\n助手：按《员工手册》考勤章节处理。"
# 与 rag.memory_service._build_memory_context / _build_memory_aware_retrieval_question 的真实输出逐字对应
EXPECTED_MEMORY_CONTEXT = f"最近对话窗口：\n{RECENT_MEMORY_TEXT}"
EXPECTED_RETRIEVAL_QUESTION = (
    "以下会话记忆仅用于消解当前问题中的指代和省略，不作为事实依据。\n"
    f"{EXPECTED_MEMORY_CONTEXT}\n\n"
    "当前检索问题：迟到怎么处理"
)
EXPECTED_CONTEXT = f"[来源: 制度.txt]\n{CHUNK_CONTENT}"
EXPECTED_PROMPT = (
    "用户问题：迟到怎么处理\n\n"
    f"会话记忆：\n{EXPECTED_MEMORY_CONTEXT}\n\n"
    f"企业知识库上下文：\n{EXPECTED_CONTEXT}"
)

TRACE_STAGES = (
    "request_received",
    "effective_question_built",
    "user_message_saved",
    "memory_built",
    "rag_gate_decided",
    "retrieval_completed",
    "context_built",
    "generation_started",
    "first_content_chunk",
    "grounding_checked",
    "assistant_message_saved",
    "ragas_scheduled",
    "memory_summary_update_scheduled",
)


def _patch_boundaries(monkeypatch, fake_db, trace_cls, *, real_generation):
    """只打桩真正的边界，返回调用记录；知识库解析/检索/生成被替换，其余协作者保持真实。"""
    calls = {"resolve": [], "retrieve": [], "stream": [], "gate": [], "ragas": [], "memory_summary": []}

    monkeypatch.setattr(chat_service, "decode_token", lambda authorization: "alice")
    monkeypatch.setattr(chat_service, "SessionLocal", lambda: fake_db)
    monkeypatch.setattr(chat_service, "TraceRecorder", trace_cls)

    def fake_resolve_knowledge_base(db, kid, user_id):
        calls["resolve"].append((db, kid, user_id))
        return FakeKnowledgeBase()

    async def fake_recent_memory_text(*_args, **_kwargs):
        return RECENT_MEMORY_TEXT

    async def fake_decide_need_rag(*args, **_kwargs):
        calls["gate"].append(args)
        return {"need_rag": True, "route": "rag", "confidence": 1.0, "source": "test", "reason": "needs retrieval"}

    async def fake_retrieve_knowledge(question, knowledge_base_id, db, trace_recorder=None):
        calls["retrieve"].append(
            {
                "question": question,
                "knowledge_base_id": knowledge_base_id,
                "db": db,
                "trace_recorder": trace_recorder,
            }
        )
        return (
            [dict(CHUNK)],
            {
                "query_plan": {"keywords": ["迟到"]},
                "routes": [{"name": "vector", "hits": 1}],
                "rrf": [],
                "rerank": {"status": "done", "items": [{"chunk_id": "a", "score": 0.9}]},
            },
        )

    monkeypatch.setattr(chat_service, "resolve_knowledge_base", fake_resolve_knowledge_base)
    monkeypatch.setattr(chat_service, "_build_recent_memory_text", fake_recent_memory_text)
    monkeypatch.setattr(chat_service, "decide_need_rag", fake_decide_need_rag)
    monkeypatch.setattr(chat_service, "retrieve_knowledge", fake_retrieve_knowledge)
    monkeypatch.setattr(chat_service, "schedule_ragas_evaluation", lambda *args: calls["ragas"].append(args))
    monkeypatch.setattr(
        chat_service,
        "_schedule_memory_summary_update",
        lambda *args: calls["memory_summary"].append(args),
    )

    if not real_generation:
        async def fake_stream_rag_answer(question, context, memory_context, trace, use_rag=True):
            calls["stream"].append(
                {
                    "question": question,
                    "context": context,
                    "memory_context": memory_context,
                    "trace": trace,
                    "use_rag": use_rag,
                }
            )
            # 回答内容从「被测代码真正拼出来的上下文」派生：上下文一旦拼错，断言立刻失败
            _, _, content = context.partition("\n")
            yield ANSWER_PREFIX
            yield content

        monkeypatch.setattr(chat_service, "stream_rag_answer", fake_stream_rag_answer)

    return calls


def _run_stream_chat(fake_db, question="迟到怎么处理"):
    response = asyncio.run(
        chat_service.stream_chat(ChatRequest(question=question), authorization="Bearer token")
    )
    return collect_stream(response.body_iterator)


def test_stream_chat_uses_direct_advanced_rag_retriever(monkeypatch, fake_db, trace_recorder_cls):
    calls = _patch_boundaries(monkeypatch, fake_db, trace_recorder_cls, real_generation=False)
    body = _run_stream_chat(fake_db)
    frames = parse_sse_frames(body)
    trace = trace_recorder_cls.instances[0]

    # 知识库解析：必须带入当前用户 id，未显式指定时回落到默认知识库
    assert calls["resolve"] == [(fake_db, None, 7)]

    # 检索入参：问题必须是「记忆感知」后的检索问题，且带上当前用户的知识库与会话
    assert len(calls["retrieve"]) == 1
    assert calls["retrieve"][0]["question"] == EXPECTED_RETRIEVAL_QUESTION
    assert calls["retrieve"][0]["knowledge_base_id"] == 3
    assert calls["retrieve"][0]["db"] is fake_db
    assert calls["retrieve"][0]["trace_recorder"] is trace

    # RAG 网关拿到的是真实记忆上下文，而不是空串
    assert calls["gate"] == [("迟到怎么处理", EXPECTED_MEMORY_CONTEXT, "制度库", [])]

    # 生成入参：context 必须由真实 chunk 拼出，memory_context 必须来自真实记忆拼接
    assert len(calls["stream"]) == 1
    assert calls["stream"][0]["question"] == "迟到怎么处理"
    assert calls["stream"][0]["context"] == EXPECTED_CONTEXT
    assert calls["stream"][0]["memory_context"] == EXPECTED_MEMORY_CONTEXT
    assert calls["stream"][0]["use_rag"] is True

    # 输出内容：SSE 流里下发的内容必须等于「由真实上下文派生」的回答
    assert streamed_content(frames) == ANSWER_TEXT
    assert body.rstrip().endswith("data: [DONE]")
    assert frames_of_type(frames, "error") == []
    assert frames_of_type(frames, "reset") == []

    conversation_frames = frames_of_type(frames, "conversation")
    assert len(conversation_frames) == 1
    conversation = conversation_frames[0]["conversation"]
    assert conversation["title"] == "迟到怎么处理"
    assert conversation["knowledge_base_id"] == 3
    assert conversation["knowledge_base_name"] == "制度库"

    # sources 帧必须等于真实 _build_sources([...CHUNK]) 的结果，而不是替身写死的字段
    sources_frames = frames_of_type(frames, "sources")
    assert len(sources_frames) == 1
    assert sources_frames[0]["sources"] == _build_sources([CHUNK])
    assert sources_frames[0]["sources"][0]["content"] == CHUNK_CONTENT

    # 帧顺序：会话信息 → 参考资料 → 内容 → 结束
    frame_kinds = [frame.get("type") or ("content" if "content" in frame else "trace") for frame in frames]
    assert frame_kinds.index("conversation") < frame_kinds.index("sources")
    assert frame_kinds.index("sources") < frame_kinds.index("content")

    # 落库：用户消息与 assistant 消息（含来源、RAGAS 状态、检索轨迹）
    user_messages = fake_db.added_by_role("user")
    assistant_messages = fake_db.added_by_role("assistant")
    assert len(user_messages) == 1
    assert user_messages[0].content == "迟到怎么处理"
    assert len(assistant_messages) == 1
    saved = assistant_messages[0]
    assert saved.content == ANSWER_TEXT
    assert saved.conversation_id == conversation["id"]
    assert saved.ragas_status == "pending"
    assert json.loads(saved.sources) == _build_sources([CHUNK])

    retrieval_trace = json.loads(saved.retrieval_trace)
    assert retrieval_trace["mode"] == "rag"
    assert retrieval_trace["effective_question"] == "迟到怎么处理"
    assert retrieval_trace["retrieval_question"] == EXPECTED_RETRIEVAL_QUESTION
    assert retrieval_trace["memory"]["used"] is True
    assert retrieval_trace["memory"]["used_for_retrieval"] is True
    assert retrieval_trace["rag_gate"]["need_rag"] is True
    assert retrieval_trace["grounding"]["status"] == "passed"
    assert retrieval_trace["grounding"]["checked_sentences"] == 1
    assert retrieval_trace["grounding"]["unsupported_count"] == 0
    assert retrieval_trace["grounding"]["unsupported_claims"] == []
    assert retrieval_trace["learning_trace"]["trace_id"] == "trace-test"
    assert retrieval_trace["learning_trace"]["event_count"] == len(trace.events)
    assert retrieval_trace["rerank"]["status"] == "done"

    # 异步任务按真实参数调度
    assert calls["ragas"] == [(saved.id, "迟到怎么处理", ANSWER_TEXT, [CHUNK_CONTENT], "trace-test")]
    assert calls["memory_summary"] == [(saved.conversation_id, "trace-test")]

    # 学习轨迹：事件由真实 _trace_sse_payloads 增量下发，结束后必须收尾
    trace_stages = [frame["event"]["stage"] for frame in frames_of_type(frames, "trace")]
    for expected_stage in TRACE_STAGES:
        assert expected_stage in trace_stages
    assert [frame["trace_id"] for frame in frames_of_type(frames, "trace")] == ["trace-test"] * len(trace_stages)
    assert trace.event("retrieval_completed")["result"] == {"final_chunks_count": 1}
    assert trace.event("context_built")["creates"]["context"] == EXPECTED_CONTEXT
    assert trace.event("first_content_chunk")["result"] == {"chunk": ANSWER_PREFIX}
    assert trace.event("grounding_checked")["result"]["status"] == "passed"
    assert trace.stages().index("retrieval_completed") < trace.stages().index("context_built")
    assert trace.attachments == {"conversation_id": conversation["id"], "message_id": saved.id}
    assert trace.finished == {
        "status": "done",
        "conversation_id": conversation["id"],
        "message_id": saved.id,
    }

    # 会话状态：新会话入库、连接归还
    conversations = [item for item in fake_db.added if isinstance(item, Conversation)]
    assert len(conversations) == 1
    assert conversations[0].id == conversation["id"]
    assert conversations[0].knowledge_base_id == 3
    assert fake_db.commits >= 3
    assert fake_db.closed is True


class _EchoContextModel:
    """把真实 prompt 里的知识库上下文回显出来，不做任何网络调用。"""

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.messages = []

    async def astream(self, messages):
        self.messages = list(messages)
        context_block = messages[1][1].split("企业知识库上下文：\n", 1)[1]
        content = context_block.split("\n", 1)[1] if "\n" in context_block else context_block
        yield SimpleNamespace(content=ANSWER_PREFIX)
        yield SimpleNamespace(content=content)


def test_stream_chat_runs_real_chain_from_retrieval_to_persisted_answer(monkeypatch, fake_db, trace_recorder_cls):
    """只替换模型入口，整条链路（chains → llm → prompt → SSE → 落库）跑真实实现。"""
    models = {}

    def fake_get_deepseek_model(**kwargs):
        model = _EchoContextModel(**kwargs)
        models["last"] = model
        return model

    monkeypatch.setattr(llm, "get_deepseek_model", fake_get_deepseek_model)
    calls = _patch_boundaries(monkeypatch, fake_db, trace_recorder_cls, real_generation=True)
    body = _run_stream_chat(fake_db)

    frames = parse_sse_frames(body)
    trace = trace_recorder_cls.instances[0]
    saved = fake_db.added_by_role("assistant")[0]

    # 真实生成链被走到（没有被假的 stream_rag_answer 顶替）
    assert calls["stream"] == []
    assert fake_db.commits >= 3
    model = models["last"]
    assert model.init_kwargs == {"streaming": True, "temperature": 0.1}

    # 真实 prompt：检索到的 chunk 必须真的进入企业知识库上下文分区
    assert [role for role, _ in model.messages] == ["system", "user"]
    assert model.messages[1][1] == EXPECTED_PROMPT
    assert "企业知识库智能问答助手" in model.messages[0][1]

    # 回答由上下文派生，并且如实下发与落库
    assert frames_of_type(frames, "error") == []
    assert streamed_content(frames) == ANSWER_TEXT
    assert saved.content == ANSWER_TEXT
    assert json.loads(saved.sources) == _build_sources([CHUNK])
    assert json.loads(saved.retrieval_trace)["grounding"]["status"] == "passed"

    prompt_event = trace.event("langchain_generation_prompt_built")
    assert prompt_event["creates"]["mode"] == "rag"
    assert prompt_event["creates"]["user_prompt"] == EXPECTED_PROMPT
