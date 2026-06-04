from __future__ import annotations

import json
import math
from typing import Any

import httpx

from config import (
    RERANK_API_KEY,
    RERANK_BASE_URL,
    RERANK_LLM_FALLBACK_ENABLED,
    RERANK_MODEL,
    RERANK_PROVIDER,
    RERANK_TIMEOUT_SECONDS,
    RETRIEVAL_RERANK_TOP_N,
)
from rag.llm import call_chat_json


async def rerank_chunks(question: str, chunks: list[dict]) -> tuple[list[dict], dict]:
    if not chunks:
        return [], {"status": "skipped", "items": []}
    try:
        return await _rerank_chunks_with_qwen3(question, chunks)
    except Exception as exc:
        if not RERANK_LLM_FALLBACK_ENABLED:
            return [], {
                "status": "failed",
                "provider": RERANK_PROVIDER,
                "model": RERANK_MODEL,
                "error": str(exc),
                "items": [],
            }
        reranked, trace = await _rerank_chunks_with_llm(question, chunks)
        if trace.get("status") == "done":
            trace["provider"] = "deepseek_fallback"
            trace["fallback_from"] = RERANK_MODEL
            trace["fallback_reason"] = str(exc)
        else:
            trace["fallback_from"] = RERANK_MODEL
            trace["fallback_reason"] = str(exc)
        return reranked, trace


async def _rerank_chunks_with_qwen3(question: str, chunks: list[dict]) -> tuple[list[dict], dict]:
    if not RERANK_API_KEY:
        raise RuntimeError("RERANK_API_KEY or DASHSCOPE_API_KEY is not configured")

    documents = [_text_value(chunk.get("content"))[:4000] for chunk in chunks]
    payload = {
        "model": RERANK_MODEL,
        "query": question,
        "documents": documents,
        "top_n": min(RETRIEVAL_RERANK_TOP_N, len(chunks)),
        "instruct": "根据用户问题判断企业知识库片段的相关性，优先选择能够直接回答问题的片段。",
    }
    headers = {
        "Authorization": f"Bearer {RERANK_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=RERANK_TIMEOUT_SECONDS) as client:
        response = await client.post(RERANK_BASE_URL, json=payload, headers=headers)
    response.raise_for_status()

    rows = _extract_qwen3_rerank_rows(response.json())
    if not rows:
        raise ValueError("qwen3-rerank response has no results")

    reranked: list[dict] = []
    trace_items: list[dict] = []
    for row in rows:
        index = _to_int(row.get("index"))
        if index is None or index < 0 or index >= len(chunks):
            continue
        score = _to_float(row.get("relevance_score", row.get("score")))
        chunk = chunks[index]
        next_chunk = {
            **chunk,
            "rerank_score": score,
            "rerank_reason": "qwen3-rerank relevance score",
        }
        reranked.append(next_chunk)
        trace_items.append(trace_chunk(next_chunk))

    if not reranked:
        raise ValueError("qwen3-rerank response indexes did not match candidates")

    reranked.sort(key=lambda item: item.get("rerank_score", 0), reverse=True)
    return reranked, {
        "status": "done",
        "provider": RERANK_PROVIDER,
        "model": RERANK_MODEL,
        "items": trace_items,
    }


def _extract_qwen3_rerank_rows(data: dict) -> list[dict]:
    if not isinstance(data, dict):
        return []
    candidates = [
        data.get("results"),
        data.get("data"),
        (data.get("output") or {}).get("results") if isinstance(data.get("output"), dict) else None,
    ]
    for rows in candidates:
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value) -> float:
    number = _to_number(value)
    return max(0.0, min(1.0, number))


def _to_number(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return number


async def _rerank_chunks_with_llm(question: str, chunks: list[dict]) -> tuple[list[dict], dict]:
    compact_candidates = [
        {
            "id": index,
            "file_name": chunk.get("file_name", ""),
            "content": _text_value(chunk.get("content"))[:700],
        }
        for index, chunk in enumerate(chunks, start=1)
    ]
    system_prompt = (
        "你是企业知识库 RAG 重排器。只输出 JSON，不要输出 Markdown。"
        "根据用户问题评估候选片段的相关性，返回字段 results，数组元素包含 id、score、reason。"
        "score 范围 0 到 1。"
    )
    user_prompt = json.dumps(
        {
            "question": question,
            "candidates": compact_candidates,
            "top_n": RETRIEVAL_RERANK_TOP_N,
        },
        ensure_ascii=False,
    )
    try:
        data = await call_chat_json(system_prompt, user_prompt, max_tokens=1200)
        rows = data.get("results") or []
        by_id = {index: chunk for index, chunk in enumerate(chunks, start=1)}
        reranked = []
        trace_items = []
        for row in rows:
            candidate_id = _to_int(row.get("id"))
            if candidate_id is None:
                continue
            chunk = by_id.get(candidate_id)
            if not chunk:
                continue
            score = _to_float(row.get("score"))
            next_chunk = {**chunk, "rerank_score": score, "rerank_reason": str(row.get("reason", ""))}
            reranked.append(next_chunk)
            trace_items.append(trace_chunk(next_chunk))
        reranked.sort(key=lambda item: _to_float(item.get("rerank_score")), reverse=True)
        return reranked, {"status": "done", "provider": "deepseek", "items": trace_items}
    except Exception as exc:
        return [], {"status": "failed", "provider": "deepseek", "error": str(exc), "items": []}


def select_final_chunks(ranked_chunks: list[dict], keyword_chunks: list[dict]) -> list[dict]:
    selected = list(ranked_chunks[:RETRIEVAL_RERANK_TOP_N])
    clean_keyword_chunks = [chunk for chunk in keyword_chunks if isinstance(chunk, dict)]
    if clean_keyword_chunks:
        best_keyword = clean_keyword_chunks[0]
        best_score = _to_number(best_keyword.get("keyword_score"))
        already_selected = any(chunk_key(chunk) == chunk_key(best_keyword) for chunk in selected)
        if best_score >= 10 and not already_selected:
            selected = [best_keyword, *selected]
    deduped = []
    seen = set()
    for chunk in selected:
        key = chunk_key(chunk)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(chunk)
    return deduped[:RETRIEVAL_RERANK_TOP_N]


def chunk_key(chunk: dict) -> str:
    return f"{chunk.get('file_id', 0)}:{chunk.get('chunk_id') or chunk.get('id')}"


def trace_chunk(chunk: dict) -> dict:
    return {
        "file_id": chunk.get("file_id", 0),
        "file_name": chunk.get("file_name", ""),
        "chunk_id": chunk.get("chunk_id", ""),
        "route": chunk.get("route", ""),
        "routes": chunk.get("routes", []),
        "rrf_score": chunk.get("rrf_score"),
        "rerank_score": chunk.get("rerank_score"),
        "rerank_reason": chunk.get("rerank_reason", ""),
        "keyword_score": chunk.get("keyword_score"),
        "excerpt": _text_value(chunk.get("content"))[:120],
    }


def _text_value(value: Any) -> str:
    return "" if value is None else str(value)
