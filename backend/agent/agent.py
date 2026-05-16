import json
import logging
from typing import Any

from langchain.agents import create_agent

from rag.llm import get_deepseek_model
from tool.tools import LANGCHAIN_RETRIEVAL_TOOLS, retrieval_runtime, retrieve_knowledge


logger = logging.getLogger(__name__)

MAX_AGENT_ROUNDS = 2
MIN_USEFUL_CHUNKS = 2
MIN_QUALITY_SCORE = 0.58


async def agentic_retrieve_knowledge(
    question: str,
    knowledge_base_id: int,
    db,
    rag_gate: dict | None = None,
    memory_context: str = "",
    trace_recorder: Any = None,
) -> tuple[list[dict], dict]:
    agent_trace = {
        "enabled": True,
        "framework": "langchain",
        "max_rounds": MAX_AGENT_ROUNDS,
        "planner": {},
        "steps": [],
        "reflections": [],
        "final": {},
    }
    _trace_add(
        trace_recorder,
        "langchain_agent_started",
        "create_agent",
        params={
            "question": question,
            "knowledge_base_id": knowledge_base_id,
            "rag_gate": rag_gate or {},
        },
        note="LangChain Agent 开始规划检索。本项目仍限制最多 2 轮，避免开放式工具循环。",
    )

    agent_output = await _run_langchain_agent(question, knowledge_base_id, db, rag_gate, memory_context, trace_recorder)
    agent_trace["planner"] = agent_output
    planned_queries = _normalize_queries(agent_output.get("queries"), question)
    max_rounds = max(1, min(MAX_AGENT_ROUNDS, int(agent_output.get("max_rounds") or 1)))

    attempts = []
    current_query = planned_queries[0]
    with retrieval_runtime(db, knowledge_base_id, trace_recorder):
        for round_index in range(1, max_rounds + 1):
            try:
                chunks, retrieval_trace = await retrieve_knowledge(
                    current_query,
                    knowledge_base_id=knowledge_base_id,
                    db=db,
                    trace_recorder=trace_recorder,
                )
            except Exception as exc:
                step = {
                    "round": round_index,
                    "tool": "retrieve_knowledge",
                    "query": current_query,
                    "status": "failed",
                    "error": str(exc),
                }
                agent_trace["steps"].append(step)
                _trace_add(
                    trace_recorder,
                    "langchain_tool_failed",
                    "retrieve_knowledge",
                    params={"round": round_index, "query": current_query},
                    result={"error": str(exc)},
                    note="LangChain 检索工具失败，Agent 停止本轮检索并返回空上下文。",
                )
                break

            step = {
                "round": round_index,
                "tool": "retrieve_knowledge",
                "query": current_query,
                "status": "done",
                "chunks_count": len(chunks),
                "routes_count": len((retrieval_trace or {}).get("routes") or []),
                "rerank_status": ((retrieval_trace or {}).get("rerank") or {}).get("status", ""),
            }
            agent_trace["steps"].append(step)
            attempts.append({
                "round": round_index,
                "query": current_query,
                "chunks": chunks,
                "retrieval_trace": retrieval_trace or {},
                "score": _quality_score(chunks, retrieval_trace or {}),
            })

            reflection = _reflect_attempt(question, current_query, chunks, retrieval_trace or {}, round_index, max_rounds)
            agent_trace["reflections"].append(reflection)
            _trace_add(
                trace_recorder,
                "langchain_agent_reflected",
                "create_agent",
                uses={
                    "round": round_index,
                    "query": current_query,
                    "chunks_count": len(chunks),
                    "rerank": (retrieval_trace or {}).get("rerank", {}),
                },
                creates={"agent_reflection": reflection},
                result={
                    "quality_score": reflection["quality_score"],
                    "should_retry": reflection["should_retry"],
                    "stop_reason": reflection["reason"],
                },
                note="LangChain Agent 对检索结果质量做有界反思，必要时最多再换一个查询。",
            )
            if not reflection["should_retry"]:
                break
            planned_retry_query = planned_queries[round_index] if len(planned_queries) > round_index else ""
            current_query = planned_retry_query or reflection["next_query"] or question

    if not attempts:
        final_trace = {
            "query_plan": {},
            "routes": [],
            "rrf": [],
            "rerank": {"status": "failed", "items": []},
        }
        selected_chunks = []
        selected_round = 0
    else:
        best = max(attempts, key=lambda item: item["score"])
        selected_chunks = best["chunks"]
        final_trace = best["retrieval_trace"]
        selected_round = best["round"]

    agent_trace["final"] = {
        "selected_round": selected_round,
        "rounds_used": len(agent_trace["steps"]),
        "chunks_count": len(selected_chunks),
        "stop_reason": _final_stop_reason(agent_trace),
    }
    final_trace["agent"] = agent_trace
    _trace_add(
        trace_recorder,
        "langchain_agent_finished",
        "create_agent",
        creates={"agent": agent_trace},
        result=agent_trace["final"],
        note="LangChain Agent 完成检索编排，选中的上下文会进入正常回答生成链。",
    )
    return selected_chunks, final_trace


async def _run_langchain_agent(
    question: str,
    knowledge_base_id: int,
    db,
    rag_gate: dict | None,
    memory_context: str,
    trace_recorder: Any,
) -> dict:
    fallback = _fallback_plan(question, "fallback")
    system_prompt = (
        "你是企业知识库问答系统中的受控 LangChain RAG Agent。"
        "你可以使用提供的检索工具，但检索计划必须保持简洁。"
        "最终只能返回一个 JSON 对象，字段只能包含：should_retrieve、queries、max_rounds、reason。"
        f"max_rounds 必须在 1 到 {MAX_AGENT_ROUNDS} 之间。"
    )
    user_prompt = json.dumps(
        {
            "question": question,
            "knowledge_base_id": knowledge_base_id,
            "memory_context": memory_context or "",
            "rag_gate": rag_gate or {},
            "rules": {
                "need_queries": "生成 1 到 2 条简洁的检索 query",
                "fallback": "如果不确定，就使用原始问题进行检索。",
            },
        },
        ensure_ascii=False,
    )
    try:
        with retrieval_runtime(db, knowledge_base_id, trace_recorder):
            agent = create_agent(
                get_deepseek_model(streaming=False, temperature=0, max_tokens=900),
                tools=LANGCHAIN_RETRIEVAL_TOOLS,
                system_prompt=system_prompt,
                name="enterprise_rag_agent",
            )
            result = await agent.ainvoke({"messages": [{"role": "user", "content": user_prompt}]})
        content = _last_message_content(result)
        plan = _normalize_plan(_parse_json(content), question)
        plan["source"] = "langchain_agent"
        _trace_add(
            trace_recorder,
            "langchain_agent_planned",
            "create_agent",
            creates={"agent_plan": plan},
            result={
                "max_rounds": plan["max_rounds"],
                "queries_count": len(plan["queries"]),
                "source": plan["source"],
            },
            note="LangChain Agent 返回结构化检索计划。",
        )
        return plan
    except Exception as exc:
        fallback["error"] = str(exc)
        _trace_add(
            trace_recorder,
            "langchain_agent_plan_failed",
            "create_agent",
            result={"error": str(exc), "fallback": fallback},
            note="LangChain Agent 规划失败，系统回退到原始问题检索。",
        )
        return fallback


def _normalize_plan(data: dict, question: str) -> dict:
    if not isinstance(data, dict):
        return _fallback_plan(question, "invalid_plan")
    queries = _normalize_queries(data.get("queries"), question)
    max_rounds = _safe_int(data.get("max_rounds"), 1)
    should_retrieve = data.get("should_retrieve")
    return {
        "should_retrieve": should_retrieve is not False,
        "queries": queries[:MAX_AGENT_ROUNDS] or [question],
        "max_rounds": max(1, min(MAX_AGENT_ROUNDS, max_rounds)),
        "reason": str(data.get("reason") or "LangChain Agent 已完成检索规划。").strip(),
        "source": "langchain_agent",
    }


def _fallback_plan(question: str, source: str) -> dict:
    return {
        "should_retrieve": True,
        "queries": [question],
        "max_rounds": 1,
        "reason": "使用稳定的原始问题检索路径。",
        "source": source,
    }


def _normalize_queries(value: Any, question: str) -> list[str]:
    if not isinstance(value, list):
        return [question]
    result = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result or [question]


def _reflect_attempt(
    question: str,
    query: str,
    chunks: list[dict],
    retrieval_trace: dict,
    round_index: int,
    max_rounds: int,
) -> dict:
    score = _quality_score(chunks, retrieval_trace)
    can_retry = round_index < max_rounds
    should_retry = can_retry and score < MIN_QUALITY_SCORE
    next_query = _retry_query(question, query, retrieval_trace) if should_retry else ""
    reason = "context accepted"
    if should_retry:
        reason = "retrieval quality is weak; try one refined query"
    elif not chunks:
        reason = "no context found; max rounds reached"
    return {
        "round": round_index,
        "quality_score": score,
        "chunks_count": len(chunks),
        "should_retry": should_retry,
        "next_query": next_query,
        "reason": reason,
    }


def _quality_score(chunks: list[dict], retrieval_trace: dict) -> float:
    if not chunks:
        return 0.0
    chunk_score = min(len(chunks), MIN_USEFUL_CHUNKS) / MIN_USEFUL_CHUNKS * 0.35
    rerank = retrieval_trace.get("rerank") or {}
    scores = [
        float(item.get("rerank_score", item.get("score", 0)) or 0)
        for item in chunks
        if isinstance(item, dict)
    ]
    rerank_score = max(scores) if scores else 0.0
    if not rerank_score:
        rerank_items = rerank.get("items") or []
        rerank_score = max(
            [float(item.get("rerank_score", item.get("score", 0)) or 0) for item in rerank_items] or [0.0]
        )
    route_count = len(retrieval_trace.get("routes") or [])
    route_score = min(route_count, 3) / 3 * 0.2
    status_bonus = 0.1 if rerank.get("status") == "done" else 0.0
    return round(min(1.0, chunk_score + rerank_score * 0.35 + route_score + status_bonus), 3)


def _retry_query(question: str, previous_query: str, retrieval_trace: dict) -> str:
    keywords = ((retrieval_trace.get("query_plan") or {}).get("keywords") or [])[:8]
    joined = " ".join(str(item) for item in keywords if str(item).strip())
    if joined and joined not in previous_query:
        return f"{question} {joined}".strip()
    return question if previous_query != question else f"{question} 相关制度 原文 条款"


def _final_stop_reason(agent_trace: dict) -> str:
    reflections = agent_trace.get("reflections") or []
    if not reflections:
        return "no_attempt_completed"
    last = reflections[-1]
    if last.get("should_retry"):
        return "max_rounds_reached"
    return str(last.get("reason") or "context accepted")


def _last_message_content(result: dict) -> str:
    messages = result.get("messages") or []
    if not messages:
        return ""
    message = messages[-1]
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content", "")
    if isinstance(content, list):
        return "".join(str(item.get("text", item)) if isinstance(item, dict) else str(item) for item in content)
    return str(content or "")


def _parse_json(content: str) -> dict:
    from rag.llm import parse_json_object

    return parse_json_object(content)


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _trace_add(trace_recorder: Any, *args, **kwargs) -> None:
    if not trace_recorder:
        return
    try:
        trace_recorder.add(*args, **kwargs)
    except Exception:
        logger.debug("Trace add failed", exc_info=True)
