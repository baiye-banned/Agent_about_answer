import json
import re
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from chroma_client import embedding_backend_status, query_vectors
from config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    RETRIEVAL_RERANK_TOP_N,
    RETRIEVAL_ROUTE_TOP_K,
)
from models import KnowledgeFile


def normalize_deepseek_model(model: str) -> str:
    aliases = {
        "deepseekv4flash": "deepseek-v4-flash",
        "deepseekv4pro": "deepseek-v4-pro",
    }
    return aliases.get((model or "").lower(), model)


def deepseek_chat_url() -> str:
    base_url = DEEPSEEK_BASE_URL.rstrip("/")
    if base_url.endswith("/v1"):
        return f"{base_url}/chat/completions"
    return f"{base_url}/v1/chat/completions"


async def call_deepseek_json(system_prompt: str, user_prompt: str, max_tokens: int = 800) -> dict:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DeepSeek API Key 未配置")

    payload = {
        "model": normalize_deepseek_model(DEEPSEEK_MODEL),
        "stream": False,
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(deepseek_chat_url(), json=payload, headers=headers)
    if response.status_code >= 400:
        raise RuntimeError(f"DeepSeek 调用失败：{response.status_code} {response.text[:200]}")

    content = response.json().get("choices", [{}])[0].get("message", {}).get("content", "")
    return _parse_json_object(content)


async def build_query_plan(question: str) -> dict:
    system_prompt = (
        "你是企业知识库 RAG 检索优化器。请只输出 JSON，不要输出 Markdown。"
        "JSON 字段为 hyde_document、rewrites、keywords。"
    )
    user_prompt = (
        "请为下面的用户问题生成："
        "1）一段可能存在于企业文档中的假设答案文档 hyde_document；"
        "2）3 个语义不同但意图一致的检索改写 rewrites；"
        "3）5 到 8 个中文关键词 keywords。\n\n"
        f"用户问题：{question}"
    )
    try:
        data = await call_deepseek_json(system_prompt, user_prompt)
    except Exception as exc:
        return {
            "hyde_document": "",
            "rewrites": [],
            "keywords": _fallback_keywords(question),
            "error": str(exc),
        }

    return {
        "hyde_document": str(data.get("hyde_document") or "").strip(),
        "rewrites": _clean_list(data.get("rewrites"))[:3],
        "keywords": (_clean_list(data.get("keywords")) or _fallback_keywords(question))[:8],
        "error": "",
    }


async def retrieve_knowledge(question: str, knowledge_base_id: int, db: Session) -> tuple[list[dict], dict]:
    query_plan = await build_query_plan(question)
    trace = {
        "embedding": embedding_backend_status(),
        "query_plan": query_plan,
        "routes": [],
        "rrf": [],
        "rerank": {"status": "skipped", "items": []},
    }

    route_results: list[tuple[str, list[dict]]] = []
    route_specs = [("original", question)]
    if query_plan.get("hyde_document"):
        route_specs.append(("hyde", query_plan["hyde_document"]))
    for index, rewrite in enumerate(query_plan.get("rewrites") or [], start=1):
        route_specs.append((f"rewrite_{index}", rewrite))

    for route, query in route_specs:
        chunks = query_vectors(
            query,
            top_k=RETRIEVAL_ROUTE_TOP_K,
            knowledge_base_id=knowledge_base_id,
            route=route,
        )
        route_results.append((route, chunks))
        trace["routes"].append({
            "route": route,
            "query": query,
            "count": len(chunks),
            "items": [_trace_chunk(item) for item in chunks[:5]],
        })

    keyword_chunks = keyword_recall(
        db,
        knowledge_base_id=knowledge_base_id,
        keywords=query_plan.get("keywords") or [],
        top_k=RETRIEVAL_ROUTE_TOP_K,
    )
    route_results.append(("keyword", keyword_chunks))
    trace["routes"].append({
        "route": "keyword",
        "query": " ".join(query_plan.get("keywords") or []),
        "count": len(keyword_chunks),
        "items": [_trace_chunk(item) for item in keyword_chunks[:5]],
    })

    fused = rrf_fuse(route_results)
    trace["rrf"] = [_trace_chunk(item) for item in fused[:10]]
    reranked, rerank_trace = await rerank_chunks(question, fused[:12])
    trace["rerank"] = rerank_trace
    final_chunks = (reranked or fused)[:RETRIEVAL_RERANK_TOP_N]
    return final_chunks, trace


def keyword_recall(db: Session, knowledge_base_id: int, keywords: list[str], top_k: int) -> list[dict]:
    clean_keywords = [keyword.lower() for keyword in keywords if keyword]
    if not clean_keywords:
        return []

    candidates = []
    files = db.query(KnowledgeFile).filter_by(knowledge_base_id=knowledge_base_id).all()
    for file_entry in files:
        content = file_entry.content or ""
        lower_content = content.lower()
        if not any(keyword in lower_content for keyword in clean_keywords):
            continue
        for chunk in _split_keyword_chunks(content):
            lower_chunk = chunk["content"].lower()
            score = sum(lower_chunk.count(keyword) for keyword in clean_keywords)
            if score <= 0:
                continue
            candidates.append({
                "id": f"{file_entry.id}_{chunk['chunk_id']}",
                "chunk_id": str(chunk["chunk_id"]),
                "content": chunk["content"],
                "file_name": file_entry.name,
                "file_id": file_entry.id,
                "route": "keyword",
                "keyword_score": score,
            })
    candidates.sort(key=lambda item: item["keyword_score"], reverse=True)
    return candidates[:top_k]


def rrf_fuse(route_results: list[tuple[str, list[dict]]], k: int = 60) -> list[dict]:
    fused: dict[str, dict] = {}
    for route, chunks in route_results:
        for rank, chunk in enumerate(chunks, start=1):
            key = _chunk_key(chunk)
            entry = fused.setdefault(key, {**chunk, "routes": [], "rrf_score": 0.0})
            entry["rrf_score"] += 1.0 / (k + rank)
            entry["routes"].append({"route": route, "rank": rank})
    return sorted(fused.values(), key=lambda item: item["rrf_score"], reverse=True)


async def rerank_chunks(question: str, chunks: list[dict]) -> tuple[list[dict], dict]:
    if not chunks:
        return [], {"status": "skipped", "items": []}

    compact_candidates = [
        {
            "id": index,
            "file_name": chunk.get("file_name", ""),
            "content": (chunk.get("content") or "")[:700],
        }
        for index, chunk in enumerate(chunks, start=1)
    ]
    system_prompt = (
        "你是企业知识库 RAG 重排器。请只输出 JSON，不要输出 Markdown。"
        "根据用户问题评估候选片段相关性，返回字段 results，数组元素包含 id、score、reason。"
        "score 为 0 到 1。"
    )
    user_prompt = json.dumps({
        "question": question,
        "candidates": compact_candidates,
        "top_n": RETRIEVAL_RERANK_TOP_N,
    }, ensure_ascii=False)
    try:
        data = await call_deepseek_json(system_prompt, user_prompt, max_tokens=1200)
        rows = data.get("results") or []
        by_id = {index: chunk for index, chunk in enumerate(chunks, start=1)}
        reranked = []
        trace_items = []
        for row in rows:
            candidate_id = int(row.get("id"))
            chunk = by_id.get(candidate_id)
            if not chunk:
                continue
            score = float(row.get("score", 0))
            next_chunk = {**chunk, "rerank_score": score, "rerank_reason": str(row.get("reason", ""))}
            reranked.append(next_chunk)
            trace_items.append(_trace_chunk(next_chunk))
        reranked.sort(key=lambda item: item.get("rerank_score", 0), reverse=True)
        return reranked, {"status": "done", "items": trace_items}
    except Exception as exc:
        return [], {"status": "failed", "error": str(exc), "items": []}


def _parse_json_object(content: str) -> dict:
    content = (content or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?", "", content).strip()
        content = re.sub(r"```$", "", content).strip()
    match = re.search(r"\{.*\}", content, flags=re.S)
    if match:
        content = match.group(0)
    return json.loads(content)


def _clean_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _fallback_keywords(question: str) -> list[str]:
    tokens = re.findall(r"[\u4e00-\u9fffA-Za-z0-9_]{2,}", question or "")
    return tokens[:8]


def _split_keyword_chunks(content: str, chunk_size: int = 500) -> list[dict]:
    chunks = []
    for start in range(0, len(content), chunk_size):
        text = content[start:start + chunk_size].strip()
        if text:
            chunks.append({"chunk_id": str(start), "content": text})
    return chunks


def _chunk_key(chunk: dict) -> str:
    return f"{chunk.get('file_id', 0)}:{chunk.get('chunk_id') or chunk.get('id')}"


def _trace_chunk(chunk: dict) -> dict:
    return {
        "file_id": chunk.get("file_id", 0),
        "file_name": chunk.get("file_name", ""),
        "chunk_id": chunk.get("chunk_id", ""),
        "route": chunk.get("route", ""),
        "routes": chunk.get("routes", []),
        "rrf_score": chunk.get("rrf_score"),
        "rerank_score": chunk.get("rerank_score"),
        "rerank_reason": chunk.get("rerank_reason", ""),
        "excerpt": (chunk.get("content") or "")[:120],
    }
