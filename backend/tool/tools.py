from __future__ import annotations

import json
import re
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from langchain.tools import tool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from rag.chroma_client import embedding_backend_status, query_vectors
from config import RETRIEVAL_RERANK_TOP_N, RETRIEVAL_ROUTE_TOP_K
from model.models import KnowledgeFile
from rag.llm import call_chat_json, call_router_json


ROUTE_CONFIDENCE_THRESHOLD = 0.55
ROUTE_TIMEOUT_SECONDS = 20


@dataclass
class RetrievalRuntime:
    db: Session
    knowledge_base_id: int
    trace_recorder: Any = None


class RAGDecisionInput(BaseModel):
    question: str
    memory_context: str = ""
    knowledge_base_name: str = ""
    attachments: list[dict] = Field(default_factory=list)


class QueryPlanInput(BaseModel):
    question: str


class RetrieveKnowledgeInput(BaseModel):
    question: str


class KeywordRecallInput(BaseModel):
    keywords: list[str]
    top_k: int = RETRIEVAL_ROUTE_TOP_K


class RerankInput(BaseModel):
    question: str
    chunks: list[dict]


@tool("decide_need_rag", args_schema=RAGDecisionInput)
async def decide_need_rag_tool(
    question: str,
    memory_context: str = "",
    knowledge_base_name: str = "",
    attachments: list[dict] | None = None,
) -> str:
    """Decide whether a user question should use enterprise knowledge-base retrieval."""
    return json.dumps(
        await decide_need_rag(question, memory_context, knowledge_base_name, attachments or []),
        ensure_ascii=False,
    )


async def decide_need_rag(
    question: str,
    memory_context: str = "",
    knowledge_base_name: str = "",
    attachments: list[dict] | None = None,
) -> dict:
    attachments = attachments or []
    payload = {
        "question": (question or "").strip(),
        "memory_context": _clip(memory_context, 1800),
        "knowledge_base_name": knowledge_base_name or "",
        "attachments_count": len(attachments),
    }
    try:
        data = await call_router_json(payload)
        return _normalize_decision(data)
    except Exception as exc:
        return _fallback_decision(f"路由模型调用失败，保守进入 RAG：{exc}")


@tool("build_query_plan", args_schema=QueryPlanInput)
async def build_query_plan_tool(question: str) -> str:
    """Build HyDE, rewrite, and keyword search plan for retrieval."""
    return json.dumps(await build_query_plan(question), ensure_ascii=False)


async def build_query_plan(question: str) -> dict:
    system_prompt = (
        "你是企业知识库 RAG 检索规划器。只输出 JSON，不要输出 Markdown。"
        "字段必须是：hyde_document、rewrites、keywords。"
    )
    user_prompt = (
        "请为下面的用户问题生成："
        "1. 一段可能出现在企业文档里的假设答案文档 hyde_document；"
        "2. 3 个语义不同但意图一致的检索改写 rewrites；"
        "3. 不超过 8 个中文关键词 keywords。"
        f"\n\n用户问题：{question}"
    )
    try:
        data = await call_chat_json(system_prompt, user_prompt)
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
        "keywords": _merge_keywords(_clean_list(data.get("keywords")), question)[:24],
        "error": "",
    }


@tool("retrieve_knowledge", args_schema=RetrieveKnowledgeInput)
async def retrieve_knowledge_tool(question: str) -> str:
    """Retrieve relevant chunks from the current runtime knowledge base."""
    runtime = _runtime()
    chunks, trace = await retrieve_knowledge(question, runtime.knowledge_base_id, runtime.db, runtime.trace_recorder)
    return json.dumps(
        {
            "chunks_count": len(chunks),
            "top_chunks": [_trace_chunk(item) for item in chunks[:5]],
            "trace": {
                "query_plan": trace.get("query_plan", {}),
                "routes_count": len(trace.get("routes") or []),
                "rerank": trace.get("rerank", {}),
            },
        },
        ensure_ascii=False,
    )


async def retrieve_knowledge(
    question: str,
    knowledge_base_id: int,
    db: Session,
    trace_recorder: Any = None,
) -> tuple[list[dict], dict]:
    _trace_add(
        trace_recorder,
        "langchain_tool_called",
        "retrieve_knowledge",
        params={"question": question, "knowledge_base_id": knowledge_base_id},
        note="LangChain Agent 调用 retrieve_knowledge 工具，进入多路召回、融合和重排。",
    )
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
        trace["routes"].append(
            {
                "route": route,
                "query": query,
                "count": len(chunks),
                "items": [_trace_chunk(item) for item in chunks[:5]],
            }
        )

    keyword_terms = _merge_keywords(query_plan.get("keywords") or [], question)
    keyword_chunks = keyword_recall(db, knowledge_base_id, keyword_terms, RETRIEVAL_ROUTE_TOP_K)
    route_results.append(("keyword", keyword_chunks))
    trace["routes"].append(
        {
            "route": "keyword",
            "query": " ".join(keyword_terms),
            "count": len(keyword_chunks),
            "items": [_trace_chunk(item) for item in keyword_chunks[:5]],
        }
    )

    fused = rrf_fuse(route_results)
    trace["rrf"] = [_trace_chunk(item) for item in fused[:10]]
    reranked, rerank_trace = await rerank_chunks(question, fused[:12])
    trace["rerank"] = rerank_trace
    final_chunks = _select_final_chunks(reranked or fused, keyword_chunks)
    _trace_add(
        trace_recorder,
        "langchain_retriever_done",
        "retrieve_knowledge",
        creates={
            "query_plan": query_plan,
            "routes": trace["routes"],
            "rrf": trace["rrf"],
            "rerank": rerank_trace,
        },
        result={"final_chunks_count": len(final_chunks)},
        note="LangChain 检索工具完成，最终 chunk 会进入回答生成链。",
    )
    return final_chunks, trace


@tool("keyword_recall", args_schema=KeywordRecallInput)
def keyword_recall_tool(keywords: list[str], top_k: int = RETRIEVAL_ROUTE_TOP_K) -> str:
    """Recall text chunks by keyword from the current runtime knowledge base."""
    runtime = _runtime()
    chunks = keyword_recall(runtime.db, runtime.knowledge_base_id, keywords, top_k)
    return json.dumps({"chunks": [_trace_chunk(item) for item in chunks]}, ensure_ascii=False)


def keyword_recall(db: Session, knowledge_base_id: int, keywords: list[str], top_k: int) -> list[dict]:
    clean_keywords = _expand_keywords(keywords)
    if not clean_keywords:
        return []
    candidates = []
    files = db.query(KnowledgeFile).filter_by(knowledge_base_id=knowledge_base_id).all()
    for file_entry in files:
        content = file_entry.content or ""
        normalized_content = _normalize_for_match(content)
        if not any(_normalize_for_match(keyword) in normalized_content for keyword in clean_keywords):
            continue
        for chunk in _split_keyword_chunks(content):
            score = _keyword_score(chunk["content"], clean_keywords)
            if score <= 0:
                continue
            candidates.append(
                {
                    "id": f"{file_entry.id}_{chunk['chunk_id']}",
                    "chunk_id": str(chunk["chunk_id"]),
                    "content": chunk["content"],
                    "file_name": file_entry.name,
                    "file_id": file_entry.id,
                    "route": "keyword",
                    "keyword_score": score,
                }
            )
    candidates.sort(key=lambda item: item["keyword_score"], reverse=True)
    return candidates[:top_k]


@tool("rrf_fuse")
def rrf_fuse_tool(route_results_json: str, k: int = 60) -> str:
    """Fuse multiple retrieval routes with reciprocal rank fusion."""
    route_results = json.loads(route_results_json)
    return json.dumps({"chunks": rrf_fuse(route_results, k)}, ensure_ascii=False)


def rrf_fuse(route_results: list[tuple[str, list[dict]]], k: int = 60) -> list[dict]:
    fused: dict[str, dict] = {}
    for route, chunks in route_results:
        for rank, chunk in enumerate(chunks, start=1):
            key = _chunk_key(chunk)
            entry = fused.setdefault(key, {**chunk, "routes": [], "rrf_score": 0.0})
            entry["rrf_score"] += 1.0 / (k + rank)
            entry["routes"].append({"route": route, "rank": rank})
    return sorted(fused.values(), key=lambda item: item["rrf_score"], reverse=True)


@tool("rerank_chunks", args_schema=RerankInput)
async def rerank_chunks_tool(question: str, chunks: list[dict]) -> str:
    """Rerank retrieved chunks against the user question."""
    reranked, trace = await rerank_chunks(question, chunks)
    return json.dumps({"chunks": [_trace_chunk(item) for item in reranked], "trace": trace}, ensure_ascii=False)


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
        "你是企业知识库 RAG 重排器。只输出 JSON，不要输出 Markdown。"
        "根据用户问题评估候选片段相关性，返回字段 results，数组元素包含 id、score、reason。"
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


LANGCHAIN_RETRIEVAL_TOOLS = [
    retrieve_knowledge_tool,
    build_query_plan_tool,
    keyword_recall_tool,
    rrf_fuse_tool,
    rerank_chunks_tool,
]

_CURRENT_RUNTIME: ContextVar[RetrievalRuntime | None] = ContextVar("langchain_retrieval_runtime", default=None)


class retrieval_runtime:
    def __init__(self, db: Session, knowledge_base_id: int, trace_recorder: Any = None):
        self.next_runtime = RetrievalRuntime(db=db, knowledge_base_id=knowledge_base_id, trace_recorder=trace_recorder)
        self.token = None

    def __enter__(self):
        self.token = _CURRENT_RUNTIME.set(self.next_runtime)
        return self.next_runtime

    def __exit__(self, exc_type, exc, tb):
        if self.token is not None:
            _CURRENT_RUNTIME.reset(self.token)
        return False


def _runtime() -> RetrievalRuntime:
    runtime = _CURRENT_RUNTIME.get()
    if runtime is None:
        raise RuntimeError("LangChain retrieval runtime is not initialized")
    return runtime


def _normalize_decision(data: dict) -> dict:
    if not isinstance(data, dict) or "need_rag" not in data:
        return _fallback_decision("路由模型未返回有效 JSON，保守进入 RAG。")
    need_rag = _to_bool(data.get("need_rag"))
    confidence = _to_confidence(data.get("confidence"))
    reason = str(data.get("reason") or "").strip() or "路由模型已完成判断。"
    if need_rag is None:
        return _fallback_decision("路由模型缺少 need_rag 布尔值，保守进入 RAG。")
    if confidence < ROUTE_CONFIDENCE_THRESHOLD:
        return _fallback_decision(f"路由模型置信度过低（{confidence:.2f}），保守进入 RAG。")
    return {
        "need_rag": need_rag,
        "route": "rag" if need_rag else "direct",
        "confidence": confidence,
        "reason": reason,
        "source": "langchain_model",
    }


def _fallback_decision(reason: str) -> dict:
    return {
        "need_rag": True,
        "route": "rag",
        "confidence": 0.0,
        "reason": reason,
        "source": "fallback",
    }


def _to_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "rag"}:
            return True
        if normalized in {"false", "no", "0", "direct"}:
            return False
    return None


def _to_confidence(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def _clip(value: str, max_chars: int) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "...(truncated)"


def _clean_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _fallback_keywords(question: str) -> list[str]:
    text = question or ""
    keywords: list[str] = []
    numeric_phrases = re.findall(r"\d+\s*(?:分钟|元|次|天|小时)(?:以内|以上|以下|内|外)?", text)
    keywords.extend(numeric_phrases)
    policy_terms = [
        "考勤",
        "迟到",
        "早退",
        "旷工",
        "处罚",
        "罚款",
        "员工",
        "分钟",
        "以内",
        "以上",
        "制度",
        "规定",
        "流程",
        "报销",
        "请假",
        "加班",
    ]
    keywords.extend(term for term in policy_terms if term in text)
    for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9_]{2,}", text):
        keywords.append(token)
        if re.search(r"[\u4e00-\u9fff]", token) and not re.search(r"\d", token) and len(token) <= 8:
            keywords.extend(token[index : index + 2] for index in range(0, max(len(token) - 1, 0)))
    return _dedupe_keywords(keywords)[:24]


def _merge_keywords(keywords: list[str], question: str) -> list[str]:
    return _dedupe_keywords([*(keywords or []), *_fallback_keywords(question)])


def _dedupe_keywords(keywords: list[str]) -> list[str]:
    result = []
    seen = set()
    for keyword in keywords:
        value = str(keyword or "").strip()
        normalized = _normalize_for_match(value)
        if len(normalized) < 2 or normalized in seen:
            continue
        seen.add(normalized)
        result.append(value)
    return result


def _expand_keywords(keywords: list[str]) -> list[str]:
    expanded = []
    for keyword in keywords:
        value = str(keyword or "").strip()
        if not value:
            continue
        expanded.append(value)
        for phrase in re.findall(r"\d+\s*(?:分钟|元|次|天|小时)(?:以内|以上|以下|内|外)?", value):
            expanded.append(phrase)
        if re.search(r"[\u4e00-\u9fff]", value) and not re.search(r"\d", value) and 2 < len(value) <= 8:
            expanded.extend(value[index : index + 2] for index in range(0, len(value) - 1))
    return _dedupe_keywords(expanded)


def _keyword_score(content: str, keywords: list[str]) -> float:
    normalized_content = _normalize_for_match(content)
    score = 0.0
    matched_positions = []
    for keyword in keywords:
        normalized_keyword = _normalize_for_match(keyword)
        if len(normalized_keyword) < 2:
            continue
        count = normalized_content.count(normalized_keyword)
        if count <= 0:
            continue
        weight = 1.0
        if re.search(r"\d", normalized_keyword):
            weight += 3.0
        if len(normalized_keyword) >= 4:
            weight += 2.0
        if normalized_keyword in {"迟到", "早退", "旷工", "罚款", "处罚", "考勤"}:
            weight += 4.0
        score += count * weight
        matched_positions.append(normalized_content.find(normalized_keyword))
    if any(term in normalized_content for term in ("考勤", "上下班")):
        score += 6.0
    if "迟到" in normalized_content and "早退" in normalized_content:
        score += 10.0
    if re.search(r"30分钟(?:以内|以上)", normalized_content):
        score += 10.0
    if "罚款50元" in normalized_content or "罚款200元" in normalized_content:
        score += 10.0
    unique_hits = len({pos for pos in matched_positions if pos >= 0})
    score += unique_hits * 1.5
    if _has_close_matches(normalized_content, keywords):
        score += 8.0
    return score


def _has_close_matches(content: str, keywords: list[str], window: int = 120) -> bool:
    positions = []
    for keyword in keywords:
        normalized_keyword = _normalize_for_match(keyword)
        if len(normalized_keyword) < 2:
            continue
        pos = content.find(normalized_keyword)
        if pos >= 0:
            positions.append(pos)
    if len(positions) < 3:
        return False
    positions.sort()
    return any(positions[index + 2] - positions[index] <= window for index in range(len(positions) - 2))


def _normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "").lower())


def _select_final_chunks(ranked_chunks: list[dict], keyword_chunks: list[dict]) -> list[dict]:
    selected = list(ranked_chunks[:RETRIEVAL_RERANK_TOP_N])
    if keyword_chunks:
        best_keyword = keyword_chunks[0]
        best_score = float(best_keyword.get("keyword_score") or 0)
        already_selected = any(_chunk_key(chunk) == _chunk_key(best_keyword) for chunk in selected)
        if best_score >= 10 and not already_selected:
            selected = [best_keyword, *selected]
    deduped = []
    seen = set()
    for chunk in selected:
        key = _chunk_key(chunk)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(chunk)
    return deduped[:RETRIEVAL_RERANK_TOP_N]


def _split_keyword_chunks(content: str, chunk_size: int = 900, chunk_overlap: int = 180) -> list[dict]:
    chunks = []
    step = max(chunk_size - chunk_overlap, 1)
    for start in range(0, len(content), step):
        text = content[start : start + chunk_size].strip()
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
        "keyword_score": chunk.get("keyword_score"),
        "excerpt": (chunk.get("content") or "")[:120],
    }


def _trace_add(trace_recorder: Any, *args, **kwargs) -> None:
    if not trace_recorder:
        return
    try:
        trace_recorder.add(*args, **kwargs)
    except Exception:
        pass
