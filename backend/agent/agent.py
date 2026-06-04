import json
import logging
import math
from typing import Any

from langchain.agents import create_agent

from config import AGENT_PLANNER_MODE
from rag.llm import call_chat_json, get_deepseek_model
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
    planner_mode = _planner_mode()
    agent_trace = {
        "enabled": True,
        "framework": "controlled_agentic_rag",
        "mode": "controlled_workflow",
        "planner_mode": planner_mode,
        "original_question": question,
        "max_rounds": MAX_AGENT_ROUNDS,
        "planner": {},
        "steps": [],
        "reflections": [],
        "final": {},
    }
    _trace_add(
        trace_recorder,
        "agentic_retrieval_started",
        "agentic_retrieve_knowledge",
        params={
            "question": question,
            "knowledge_base_id": knowledge_base_id,
            "rag_gate": rag_gate or {},
            "planner_mode": planner_mode,
        },
        note="Agentic 检索先用有边界的查询规划器整理问题，再进入 retrieve_knowledge。",
    )

    agent_output = await _run_agent_planner(
        question,
        knowledge_base_id,
        db,
        rag_gate,
        memory_context,
        trace_recorder,
    )
    agent_trace["planner"] = agent_output
    planned_queries = _normalize_queries(agent_output.get("queries"), question)
    max_rounds = max(1, min(MAX_AGENT_ROUNDS, int(agent_output.get("max_rounds") or 1)))

    attempts = []
    current_query = planned_queries[0]
    with retrieval_runtime(db, knowledge_base_id, trace_recorder):
        for round_index in range(1, max_rounds + 1):
            round_plan = _query_plan_for_round(agent_output, current_query, round_index)
            try:
                chunks, retrieval_trace = await retrieve_knowledge(
                    current_query,
                    knowledge_base_id=knowledge_base_id,
                    db=db,
                    trace_recorder=trace_recorder,
                    query_plan=round_plan,
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
                    "agentic_retrieval_tool_failed",
                    "retrieve_knowledge",
                    params={"round": round_index, "query": current_query},
                    result={"error": str(exc)},
                    note="retrieve_knowledge 失败，Agentic 检索返回空上下文。",
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
            attempts.append(
                {
                    "round": round_index,
                    "query": current_query,
                    "chunks": chunks,
                    "retrieval_trace": retrieval_trace or {},
                    "score": _quality_score(chunks, retrieval_trace or {}),
                }
            )

            reflection = _reflect_attempt(question, current_query, chunks, retrieval_trace or {}, round_index, max_rounds)
            agent_trace["reflections"].append(reflection)
            _trace_add(
                trace_recorder,
                "agentic_retrieval_reflected",
                "agentic_retrieve_knowledge",
                uses={
                    "round": round_index,
                    "query": current_query,
                    "chunks_count": len(chunks),
                    "rerank": (retrieval_trace or {}).get("rerank", {}),
                },
                creates={"reflection": reflection},
                result={
                    "quality_score": reflection["quality_score"],
                    "should_retry": reflection["should_retry"],
                    "next_query": reflection["next_query"],
                },
                note="Agentic 检索会反思结果质量，必要时只做一次有边界的重试。",
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
            "reflection": {"should_retry": False, "next_query": ""},
            "grounding": {"status": "not_checked"},
        }
        selected_chunks = []
        selected_round = 0
    else:
        best = max(attempts, key=lambda item: item["score"])
        selected_chunks = best["chunks"]
        final_trace = best["retrieval_trace"]
        selected_round = best["round"]

    final_trace["reflection"] = (agent_trace.get("reflections") or [{"should_retry": False, "next_query": ""}])[-1]
    final_trace.setdefault("grounding", {"status": "not_checked"})
    agent_trace["final"] = {
        "selected_round": selected_round,
        "rounds_used": len(agent_trace["steps"]),
        "chunks_count": len(selected_chunks),
        "stop_reason": _final_stop_reason(agent_trace),
    }
    final_trace["agent"] = agent_trace
    _trace_add(
        trace_recorder,
        "agentic_retrieval_finished",
        "agentic_retrieve_knowledge",
        creates={"agent": agent_trace},
        result=agent_trace["final"],
        note="Agentic 检索结束，选中的上下文会进入回答生成链。",
    )
    return selected_chunks, final_trace


async def _run_agent_planner(
    question: str,
    knowledge_base_id: int,
    db,
    rag_gate: dict | None,
    memory_context: str,
    trace_recorder: Any,
) -> dict:
    mode = _planner_mode()
    if mode == "langchain":
        return await _run_langchain_agent(question, knowledge_base_id, db, rag_gate, memory_context, trace_recorder)
    if mode == "fallback":
        return _fallback_plan(question, "fallback_mode")
    return await _run_controlled_query_planner(question, rag_gate, memory_context, trace_recorder)


async def _run_controlled_query_planner(
    question: str,
    rag_gate: dict | None,
    memory_context: str,
    trace_recorder: Any,
) -> dict:
    fallback = _fallback_plan(question, "controlled_fallback")
    system_prompt = (
        "你是企业知识库的受控 Agentic RAG 查询规划器。只输出 JSON，不要输出 Markdown。"
        "不要回答用户问题，只规划应该如何检索企业知识。"
        "必填字段：simplified_question、sub_questions、rewrites、keywords、"
        "required_evidence、max_rounds、reason。max_rounds 必须在 1 到 2 之间。"
    )
    user_prompt = json.dumps(
        {
            "question": question,
            "memory_context": memory_context or "",
            "rag_gate": rag_gate or {},
            "rules": {
                "simplified_question": "改写成最简洁、最适合检索的查询句。",
                "sub_questions": "最多 3 个聚焦的检索子问题。",
                "rewrites": "最多 3 个语义一致但表达不同的检索改写。",
                "keywords": "最多 12 个可能出现在原始文档里的关键词。",
                "required_evidence": "最多 6 个安全回答所需的证据点。",
                "fallback": "如果不确定，就复用原问题。",
            },
        },
        ensure_ascii=False,
    )
    try:
        data = await call_chat_json(system_prompt, user_prompt, max_tokens=1000)
        plan = _normalize_plan(data, question)
        plan["source"] = "controlled_query_planner"
        _trace_add(
            trace_recorder,
            "controlled_query_planner_done",
            "controlled_query_planner",
            creates={"agent_plan": plan},
            result={
                "simplified_question": plan.get("simplified_question", ""),
                "sub_questions_count": len(plan.get("sub_questions") or []),
                "queries_count": len(plan.get("queries") or []),
            },
            note="受控规划器在检索前完成问题简化和拆分。",
        )
        return plan
    except Exception as exc:
        fallback["error"] = str(exc)
        _trace_add(
            trace_recorder,
            "controlled_query_planner_failed",
            "controlled_query_planner",
            result={"error": str(exc), "fallback": fallback},
            note="受控规划器失败，兜底使用带记忆上下文的问题进入检索。",
        )
        return fallback


async def _run_langchain_agent(
    question: str,
    knowledge_base_id: int,
    db,
    rag_gate: dict | None,
    memory_context: str,
    trace_recorder: Any,
) -> dict:
    fallback = _fallback_plan(question, "langchain_fallback")
    system_prompt = (
        "你是企业知识库里的有边界 LangChain RAG Agent。"
        "只输出 JSON，字段必须包含：should_retrieve、queries、max_rounds、reason。"
        f"max_rounds 必须在 1 到 {MAX_AGENT_ROUNDS} 之间。"
    )
    user_prompt = json.dumps(
        {
            "question": question,
            "knowledge_base_id": knowledge_base_id,
            "memory_context": memory_context or "",
            "rag_gate": rag_gate or {},
            "rules": {
                "need_queries": "生成 1 到 2 个简洁的检索查询。",
                "fallback": "如果不确定，就使用原问题。",
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
            note="LangChain Agent 返回了结构化检索计划。",
        )
        return plan
    except Exception as exc:
        fallback["error"] = str(exc)
        _trace_add(
            trace_recorder,
            "langchain_agent_plan_failed",
            "create_agent",
            result={"error": str(exc), "fallback": fallback},
            note="LangChain Agent 规划失败，兜底使用带记忆上下文的问题进入检索。",
        )
        return fallback


def _normalize_plan(data: dict, question: str) -> dict:
    if not isinstance(data, dict):
        return _fallback_plan(question, "invalid_plan")
    simplified_question = str(data.get("simplified_question") or "").strip()
    explicit_queries = _normalize_queries(data.get("queries"), question)
    if not simplified_question:
        simplified_question = explicit_queries[0] if explicit_queries else question
    sub_questions = _clean_list(data.get("sub_questions"))[:3]
    rewrites = _clean_list(data.get("rewrites"))[:3]
    keywords = _clean_list(data.get("keywords"))[:12]
    required_evidence = _clean_list(data.get("required_evidence"))[:6]
    queries = _dedupe_texts([simplified_question, *sub_questions, *rewrites, *explicit_queries])[:6]
    max_rounds = _safe_int(data.get("max_rounds"), 1)
    should_retrieve = data.get("should_retrieve")
    return {
        "should_retrieve": should_retrieve is not False,
        "original_question": question,
        "simplified_question": simplified_question or question,
        "sub_questions": sub_questions,
        "rewrites": rewrites,
        "keywords": keywords,
        "required_evidence": required_evidence,
        "queries": queries or [question],
        "max_rounds": max(1, min(MAX_AGENT_ROUNDS, max_rounds)),
        "reason": str(data.get("reason") or "受控规划器生成了检索计划。").strip(),
        "source": "controlled_query_planner",
    }


def _fallback_plan(question: str, source: str) -> dict:
    return {
        "should_retrieve": True,
        "original_question": question,
        "simplified_question": question,
        "sub_questions": [],
        "rewrites": [],
        "keywords": [],
        "required_evidence": [],
        "queries": [question],
        "max_rounds": 1,
        "reason": "使用带记忆上下文的问题作为稳定兜底计划。",
        "source": source,
    }


def _query_plan_for_round(plan: dict, query: str, round_index: int) -> dict:
    next_plan = dict(plan or {})
    next_plan["round"] = round_index
    next_plan["simplified_question"] = query
    existing_queries = next_plan.get("queries")
    if not isinstance(existing_queries, list):
        existing_queries = []
    next_plan["queries"] = _normalize_queries([query, *existing_queries], query)
    if round_index > 1:
        next_plan["sub_questions"] = []
    return next_plan


def _normalize_queries(value: Any, question: str) -> list[str]:
    if not isinstance(value, list):
        return [question]
    return _dedupe_texts([_text_value(item).strip() for item in value] or [question]) or [question]


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text_value(item).strip() for item in value if _text_value(item).strip()]


def _dedupe_texts(values: list[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        text = _text_value(value).strip()
        if not text:
            continue
        normalized = " ".join(text.lower().split())
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(text)
    return result


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
    reason = "检索上下文质量可接受"
    if should_retry:
        reason = "检索质量偏弱，尝试一次改写查询"
    elif not chunks:
        reason = "没有找到可用上下文，已达到最大轮次"
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
    if not isinstance(retrieval_trace, dict):
        retrieval_trace = {}
    chunk_score = min(len(chunks), MIN_USEFUL_CHUNKS) / MIN_USEFUL_CHUNKS * 0.35
    rerank = retrieval_trace.get("rerank") or {}
    scores = [
        _safe_score(item.get("rerank_score", item.get("score", 0)))
        for item in chunks
        if isinstance(item, dict)
    ]
    rerank_score = max(scores) if scores else 0.0
    if not rerank_score:
        rerank_items = rerank.get("items") or []
        rerank_score = max(
            [
                _safe_score(item.get("rerank_score", item.get("score", 0)))
                for item in rerank_items
                if isinstance(item, dict)
            ]
            or [0.0]
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
    return str(last.get("reason") or "检索上下文质量可接受")


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
    return _text_value(content)


def _parse_json(content: str) -> dict:
    from rag.llm import parse_json_object

    return parse_json_object(content)


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text_value(value: Any) -> str:
    return "" if value is None else str(value)


def _safe_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(score):
        return 0.0
    return score


def _planner_mode() -> str:
    mode = (AGENT_PLANNER_MODE or "controlled").strip().lower()
    if mode not in {"controlled", "langchain", "fallback"}:
        return "controlled"
    return mode


def _trace_add(trace_recorder: Any, *args, **kwargs) -> None:
    if not trace_recorder:
        return
    try:
        trace_recorder.add(*args, **kwargs)
    except Exception:
        logger.debug("Trace add failed", exc_info=True)
