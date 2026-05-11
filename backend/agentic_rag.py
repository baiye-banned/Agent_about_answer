import json
import re
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

from config import (
    DEEPSEEK_API_KEY,
    TEXT_FALLBACK_API_KEY,
    TEXT_FALLBACK_BASE_URL,
    TEXT_FALLBACK_MODEL,
)
from tool import call_deepseek_json, call_tool, get_tool, normalize_deepseek_model
from config import DEEPSEEK_MODEL


MAX_AGENT_ROUNDS = 2
MIN_USEFUL_CHUNKS = 2
MIN_QUALITY_SCORE = 0.58
AGENT_TIMEOUT_SECONDS = 25


async def agentic_retrieve_knowledge(
    question: str,
    knowledge_base_id: int,
    db: Session,
    rag_gate: Optional[dict] = None,
    memory_context: str = "",
    trace_recorder: Any = None,
) -> tuple[list[dict], dict]:
    """Run a bounded AgenticRAG loop over the existing retrieval pipeline."""
    agent_trace = {
        "enabled": True,
        "max_rounds": MAX_AGENT_ROUNDS,
        "planner": {},
        "steps": [],
        "reflections": [],
        "final": {},
    }
    plan = await _build_agent_plan(question, memory_context, rag_gate or {})
    agent_trace["planner"] = plan
    retrieval_tool = get_tool("retrieve_knowledge")
    _trace_add(
        trace_recorder,
        "agent_planned",
        "agentic_retrieve_knowledge",
        params={
            "question": question,
            "knowledge_base_id": knowledge_base_id,
            "rag_gate": rag_gate or {},
        },
        creates={"agent_plan": plan},
        result={
            "max_rounds": plan["max_rounds"],
            "queries_count": len(plan["queries"]),
            "source": plan["source"],
        },
        note="AgenticRAG planner created a bounded retrieval plan before calling tools.",
    )

    attempts = []
    planned_queries = plan["queries"] or [question]
    current_query = plan["queries"][0] if plan["queries"] else question
    max_rounds = max(1, min(MAX_AGENT_ROUNDS, int(plan.get("max_rounds") or 1)))

    for round_index in range(1, max_rounds + 1):
        _trace_add(
            trace_recorder,
            "agent_tool_called",
            retrieval_tool.name,
            params={
                "round": round_index,
                "tool": retrieval_tool.name,
                "query": current_query,
                "knowledge_base_id": knowledge_base_id,
            },
            note="AgenticRAG calls the existing retrieval tool. The tool itself still performs query planning, vector search, keyword recall, RRF, and rerank.",
        )
        try:
            chunks, retrieval_trace = await call_tool(
                retrieval_tool.name,
                current_query,
                knowledge_base_id=knowledge_base_id,
                db=db,
            )
        except Exception as exc:
            step = {
                "round": round_index,
                "tool": retrieval_tool.name,
                "query": current_query,
                "status": "failed",
                "error": str(exc),
            }
            agent_trace["steps"].append(step)
            _trace_add(
                trace_recorder,
                "agent_tool_failed",
                retrieval_tool.name,
                params={"round": round_index, "query": current_query},
                result={"error": str(exc)},
                note="AgenticRAG retrieval tool failed. The loop stops and returns an empty context instead of blocking the chat stream.",
            )
            break

        step = {
            "round": round_index,
            "tool": retrieval_tool.name,
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
            "agent_reflected",
            "agentic_retrieve_knowledge",
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
            note="AgenticRAG checks whether the retrieved context is strong enough or needs one more bounded retry.",
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
        "agent_finished",
        "agentic_retrieve_knowledge",
        creates={"agent": agent_trace},
        result=agent_trace["final"],
        note="AgenticRAG finished. The selected retrieval trace is passed to the normal answer generation flow.",
    )
    return selected_chunks, final_trace


async def _build_agent_plan(question: str, memory_context: str, rag_gate: dict) -> dict:
    fallback = _fallback_agent_plan(question, "fallback")
    system_prompt = (
        "You are a bounded AgenticRAG planner for an enterprise knowledge-base QA system. "
        "Return JSON only. Allowed tools are: retrieve_knowledge, memory_context, image_analysis. "
        "Do not invent tools. The plan must be safe and small."
    )
    user_prompt = json.dumps(
        {
            "question": question,
            "memory_context": _clip(memory_context, 1200),
            "rag_gate": rag_gate,
            "rules": {
                "max_rounds": MAX_AGENT_ROUNDS,
                "need_queries": "1 or 2 concise retrieval queries",
                "fallback": "If uncertain, retrieve with the original question.",
            },
            "output_schema": {
                "should_retrieve": True,
                "queries": ["string"],
                "max_rounds": 1,
                "reason": "string",
            },
        },
        ensure_ascii=False,
    )
    try:
        data = await _call_agent_json(system_prompt, user_prompt)
        plan = _normalize_agent_plan(data, question)
        plan["source"] = "model"
        return plan
    except Exception as exc:
        fallback["error"] = str(exc)
        return fallback


async def _call_agent_json(system_prompt: str, user_prompt: str) -> dict:
    if TEXT_FALLBACK_API_KEY:
        return await _call_openai_compatible_json(
            _openai_chat_url(TEXT_FALLBACK_BASE_URL),
            TEXT_FALLBACK_API_KEY,
            TEXT_FALLBACK_MODEL,
            system_prompt,
            user_prompt,
        )
    if DEEPSEEK_API_KEY:
        return await call_deepseek_json(system_prompt, user_prompt, max_tokens=700)
    raise RuntimeError("No planner model API key configured")


async def _call_openai_compatible_json(
    url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> dict:
    request_body = {
        "model": normalize_deepseek_model(model),
        "stream": False,
        "temperature": 0,
        "max_tokens": 700,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=AGENT_TIMEOUT_SECONDS) as client:
        response = await client.post(url, json=request_body, headers=headers)
    if response.status_code >= 400:
        raise RuntimeError(f"{response.status_code} {response.text[:200]}")
    content = response.json().get("choices", [{}])[0].get("message", {}).get("content", "")
    return _parse_json_object(content)


def _normalize_agent_plan(data: dict, question: str) -> dict:
    if not isinstance(data, dict):
        return _fallback_agent_plan(question, "invalid_plan")
    queries = _clean_queries(data.get("queries"), question)
    max_rounds = _safe_int(data.get("max_rounds"), 1)
    should_retrieve = data.get("should_retrieve")
    if should_retrieve is False and not queries:
        queries = [question]
    return {
        "should_retrieve": should_retrieve is not False,
        "queries": queries[:MAX_AGENT_ROUNDS] or [question],
        "max_rounds": max(1, min(MAX_AGENT_ROUNDS, max_rounds)),
        "reason": str(data.get("reason") or "Agent planner completed.").strip(),
        "source": "model",
    }


def _fallback_agent_plan(question: str, source: str) -> dict:
    return {
        "should_retrieve": True,
        "queries": [question],
        "max_rounds": 1,
        "reason": "Use the stable retrieval path.",
        "source": source,
    }


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


def _clean_queries(value: Any, question: str) -> list[str]:
    if not isinstance(value, list):
        return [question]
    result = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result or [question]


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_json_object(content: str) -> dict:
    content = (content or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?", "", content).strip()
        content = re.sub(r"```$", "", content).strip()
    match = re.search(r"\{.*\}", content, flags=re.S)
    if match:
        content = match.group(0)
    return json.loads(content)


def _openai_chat_url(base_url: str) -> str:
    base_url = (base_url or "").rstrip("/")
    if base_url.endswith("/v1"):
        return f"{base_url}/chat/completions"
    return f"{base_url}/v1/chat/completions"


def _clip(value: str, max_chars: int) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "...(truncated)"


def _trace_add(trace_recorder: Any, *args, **kwargs) -> None:
    if not trace_recorder:
        return
    try:
        trace_recorder.add(*args, **kwargs)
    except Exception:
        pass
