from __future__ import annotations

import asyncio
import logging
import math
import re
from typing import Any

from sqlalchemy.orm import Session

from config import RETRIEVAL_ROUTE_TOP_K
from model.models import KnowledgeFile
from rag.llm import call_chat_json, call_router_json
from rag.milvus_client import EmbeddingBackendError, embedding_backend_status, query_vectors
from rag.rerank import (
    chunk_content_key,
    chunk_key,
    chunk_scheme,
    rerank_chunks,
    select_final_chunks,
    trace_chunk,
)


ROUTE_CONFIDENCE_THRESHOLD = 0.55

logger = logging.getLogger(__name__)


async def decide_need_rag(
    question: str,
    memory_context: str = "",
    knowledge_base_name: str = "",
    attachments: list[dict] | None = None,
) -> dict:
    attachments = attachments or []
    payload = {
        "question": ("" if question is None else str(question)).strip(),
        "memory_context": _clip(memory_context, 1800),
        "knowledge_base_name": "" if knowledge_base_name is None else str(knowledge_base_name),
        "attachments_count": len(attachments),
    }
    try:
        data = await call_router_json(payload)
        return _normalize_decision(data)
    except Exception as exc:
        # reason 会进 SSE 轨迹帧、retrieval_trace 与消息负载，异常原文只落日志。
        logger.warning("Route model call failed, falling back to RAG: %s", exc, exc_info=True)
        return _fallback_decision("路由模型调用失败，保守进入 RAG。")


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
        # query_plan 会进 SSE 轨迹帧与 retrieval_trace；error 只作失败标记，不携带异常原文。
        logger.warning("Query plan build failed: %s", exc, exc_info=True)
        return {
            "hyde_document": "",
            "rewrites": [],
            "keywords": _fallback_keywords(question),
            "error": "查询规划失败",
        }
    return {
        "hyde_document": str(data.get("hyde_document") or "").strip(),
        "rewrites": _clean_list(data.get("rewrites"))[:3],
        "keywords": _merge_keywords(_clean_list(data.get("keywords")), question)[:24],
        "error": "",
    }


async def retrieve_knowledge(
    question: str,
    knowledge_base_id: int,
    db: Session,
    trace_recorder: Any = None,
    query_plan: dict | None = None,
) -> tuple[list[dict], dict]:
    _trace_add(
        trace_recorder,
        "retriever_started",
        "retrieve_knowledge",
        params={"question": question, "knowledge_base_id": knowledge_base_id},
        note="开始执行 retrieve_knowledge，进入多路召回、融合和重排。",
    )
    query_plan = _normalize_external_query_plan(query_plan, question) if query_plan else await build_query_plan(question)
    trace = {
        "embedding": embedding_backend_status(),
        "query_plan": query_plan,
        "routes": [],
        "rrf": [],
        "rerank": {"status": "skipped", "items": []},
    }

    route_results: list[tuple[str, list[dict]]] = []
    route_specs = _build_route_specs(question, query_plan)

    keyword_terms = _merge_keywords(
        [
            *(query_plan.get("keywords") or []),
            *(query_plan.get("required_evidence") or []),
        ],
        question,
    )

    # query_vectors / keyword_recall 都是同步实现（阻塞式网络与数据库 IO），直接在协程里
    # 调用会独占事件循环，一轮多路检索期间整个进程无法处理其它请求。注意 asyncio.to_thread
    # 返回的是惰性 coroutine：只赋值不交给事件循环并不会让它先跑起来，所以所有召回（含关键字）
    # 必须放进同一个 gather，否则关键字召回仍会排在向量召回之后串行执行。
    # (route, query, 召回调用) 三元组把描述信息与调用绑在一起，结果按描述符配对，
    # 不依赖「关键词结果恒为列表最后一个元素」的位置约定。
    recall_specs: list[tuple[str, str, Any]] = [
        (
            route,
            query,
            asyncio.to_thread(
                query_vectors,
                query,
                top_k=RETRIEVAL_ROUTE_TOP_K,
                knowledge_base_id=knowledge_base_id,
                route=route,
            ),
        )
        for route, query in route_specs
    ]
    if keyword_terms:
        recall_specs.append(
            (
                "keyword",
                " ".join(keyword_terms),
                asyncio.to_thread(keyword_recall, db, knowledge_base_id, keyword_terms, RETRIEVAL_ROUTE_TOP_K),
            )
        )

    # return_exceptions=True 是「并发」与「逐路降级」的交点：向量化后端不可用时只让该路拿到
    # EmbeddingBackendError，其余路与关键词路照常返回；同时 gather 会等全部召回结束才返回，
    # 不会留下仍在触碰请求级 Session 的孤儿线程。非 EmbeddingBackendError 的异常仍按原语义上抛。
    recall_results = await asyncio.gather(*(spec[2] for spec in recall_specs), return_exceptions=True)

    keyword_chunks: list[dict] = []
    for (route, query, _), result in zip(recall_specs, recall_results, strict=True):
        if isinstance(result, EmbeddingBackendError):
            # 查询向量化失败时绝不退化为哈希向量（会造成跨空间检索），
            # 显式跳过该路并记录原因，后续仍可用关键词路由召回。
            logger.warning("Vector route skipped, embedding backend unavailable: route=%s error=%s", route, result)
            trace["embedding_error"] = str(result)
            result = []
        elif isinstance(result, BaseException):
            raise result
        chunks: list[dict] = result
        # 关键词路复用固定名字 "keyword"（_build_route_specs 只产出
        # planned/simplified/sub_question_N/hyde/rewrite_N），按名字分流而非按下标。
        # 关键词路同样进入 route_results 参与 RRF 融合，并且始终排在向量路之后
        # （recall_specs 末尾追加），与两侧既有语义一致。
        if route == "keyword":
            keyword_chunks = chunks
        route_results.append((route, chunks))
        trace["routes"].append(
            {
                "route": route,
                "query": query,
                "count": len(chunks),
                "items": [trace_chunk(item) for item in chunks[:5]],
            }
        )

    if trace.get("embedding_error"):
        trace["embedding"] = embedding_backend_status()

    fused = rrf_fuse(route_results)
    trace["rrf"] = [trace_chunk(item) for item in fused[:10]]
    ranking_question = query_plan.get("original_question") or question
    reranked, rerank_trace = await rerank_chunks(ranking_question, fused[:12])
    trace["rerank"] = rerank_trace
    final_chunks = select_final_chunks(reranked or fused, keyword_chunks)
    _trace_add(
        trace_recorder,
        "retriever_done",
        "retrieve_knowledge",
        creates={
            "query_plan": query_plan,
            "routes": trace["routes"],
            "rrf": trace["rrf"],
            "rerank": rerank_trace,
        },
        result={"final_chunks_count": len(final_chunks)},
        note="retrieve_knowledge 完成，最终 chunk 会进入回答生成链。",
    )
    return final_chunks, trace


def _normalize_external_query_plan(plan: dict | None, question: str) -> dict:
    if not isinstance(plan, dict):
        return {
            "hyde_document": "",
            "rewrites": [],
            "keywords": _fallback_keywords(question),
            "error": "external query_plan is invalid",
        }
    question_text = _text_value(question).strip()
    simplified_question = _text_value(plan.get("simplified_question")).strip() or question_text
    rewrites = _clean_list(plan.get("rewrites"))[:3]
    sub_questions = _clean_list(plan.get("sub_questions"))[:3]
    required_evidence = _clean_list(plan.get("required_evidence"))[:6]
    keywords = _merge_keywords(_clean_list(plan.get("keywords")), " ".join([question_text, simplified_question]))
    return {
        **plan,
        "original_question": _text_value(plan.get("original_question")).strip() or question_text,
        "simplified_question": simplified_question or question_text,
        "sub_questions": sub_questions,
        "hyde_document": _text_value(plan.get("hyde_document")).strip(),
        "rewrites": rewrites,
        "keywords": keywords[:24],
        "required_evidence": required_evidence,
        "error": _text_value(plan.get("error")),
    }


def _build_route_specs(question: str, query_plan: dict) -> list[tuple[str, str]]:
    route_specs: list[tuple[str, str]] = []
    _append_route(route_specs, "planned", question)
    simplified_question = _text_value(query_plan.get("simplified_question")).strip()
    if simplified_question and simplified_question != question:
        _append_route(route_specs, "simplified", simplified_question)
    for index, sub_question in enumerate(query_plan.get("sub_questions") or [], start=1):
        _append_route(route_specs, f"sub_question_{index}", sub_question)
    if query_plan.get("hyde_document"):
        _append_route(route_specs, "hyde", query_plan["hyde_document"])
    for index, rewrite in enumerate(query_plan.get("rewrites") or [], start=1):
        _append_route(route_specs, f"rewrite_{index}", rewrite)
    return route_specs


def _append_route(route_specs: list[tuple[str, str]], route: str, query: str) -> None:
    text = _text_value(query).strip()
    if not text:
        return
    normalized = _normalize_for_match(text)
    if any(_normalize_for_match(existing_query) == normalized for _, existing_query in route_specs):
        return
    route_specs.append((route, text))


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
            # 文件级命中不代表每个窗口都相关：窗口自己也得命中本次查询关键词，
            # 否则同一份文档里与提问无关的段落会以「关键字候选」的身份进入上下文。
            matched_keywords = _matched_query_keywords(chunk["content"], clean_keywords)
            if not matched_keywords:
                continue
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
                    # 命中证据随候选一起带出，供 select_final_chunks 校验（issue #56）。
                    "keyword_hits": len(matched_keywords),
                    "matched_keywords": matched_keywords,
                }
            )
    candidates.sort(key=lambda item: item["keyword_score"], reverse=True)
    return candidates[:top_k]


def rrf_fuse(route_results: list[tuple[str, list[dict]]], k: int = 60) -> list[dict]:
    fused: dict[str, dict] = {}
    # 两套分块方案给出同一段文本时（短文档整篇就是 offset=0 的那个关键字窗口）也要并成
    # 一条：否则同一段文字会以两条候选的身份走到重排，白占一个重排名额与上下文配额，
    # 「两条路由都召回了它」这个 RRF 信号也随之丢掉。
    content_owner: dict[str, tuple[str, str]] = {}
    for route_entry in route_results:
        if not isinstance(route_entry, (list, tuple)) or len(route_entry) != 2:
            continue
        route, chunks = route_entry
        if not isinstance(chunks, list):
            continue
        for rank, chunk in enumerate(chunks, start=1):
            if not isinstance(chunk, dict):
                continue
            key = chunk_key(chunk)
            scheme = chunk_scheme(chunk)
            content = chunk_content_key(chunk)
            if content:
                owner = content_owner.get(content)
                if owner is not None and owner[0] != scheme:
                    key = owner[1]
            entry = fused.setdefault(key, {**chunk, "routes": [], "rrf_score": 0.0})
            if content:
                content_owner.setdefault(content, (scheme, key))
            entry["rrf_score"] += 1.0 / (k + rank)
            entry["routes"].append({"route": route, "rank": rank})
    return sorted(fused.values(), key=lambda item: item["rrf_score"], reverse=True)


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
        "source": "router_model",
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
    if not math.isfinite(number):
        return 0.0
    return max(0.0, min(1.0, number))


def _clip(value: str, max_chars: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "...(truncated)"


def _text_value(value: Any) -> str:
    return "" if value is None else str(value)


def _clean_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _fallback_keywords(question: str) -> list[str]:
    text = _text_value(question)
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
        value = _text_value(keyword).strip()
        normalized = _normalize_for_match(value)
        if len(normalized) < 2 or normalized in seen:
            continue
        seen.add(normalized)
        result.append(value)
    return result


def _expand_keywords(keywords: list[str]) -> list[str]:
    expanded = []
    for keyword in keywords:
        value = _text_value(keyword).strip()
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
    # 这里不再按内容自身的特征短语（考勤/迟到+早退/30分钟以内/罚款50元…）无条件加分：
    # 那与「本次查询问的是什么」无关，会让整段跑题内容拿到高分并被插到上下文首位。
    # 相关性只由上面的「命中本次查询关键词」决定，零命中内容得 0 分。
    unique_hits = len({pos for pos in matched_positions if pos >= 0})
    score += unique_hits * 1.5
    if _has_close_matches(normalized_content, keywords):
        score += 8.0
    return score


def _matched_query_keywords(content: str, keywords: list[str]) -> list[str]:
    """返回内容里真正命中的查询关键词（保持传入顺序、按归一化形式去重）。

    issue #56：窗口是否与本次提问相关，只由这个命中集合决定；内容自身的场景
    特征词（考勤、迟到、罚款…）不构成相关性证据。
    """
    normalized_content = _normalize_for_match(content)
    matched: list[str] = []
    seen: set[str] = set()
    for keyword in keywords:
        normalized_keyword = _normalize_for_match(keyword)
        if len(normalized_keyword) < 2 or normalized_keyword in seen:
            continue
        if normalized_keyword in normalized_content:
            seen.add(normalized_keyword)
            matched.append(keyword)
    return matched


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
    value = "" if text is None else str(text)
    return re.sub(r"\s+", "", value.lower())


def _split_keyword_chunks(content: str, chunk_size: int = 900, chunk_overlap: int = 180) -> list[dict]:
    chunks = []
    step = max(chunk_size - chunk_overlap, 1)
    for start in range(0, len(content), step):
        text = content[start : start + chunk_size].strip()
        if text:
            chunks.append({"chunk_id": str(start), "content": text})
    return chunks


def _trace_add(trace_recorder: Any, *args, **kwargs) -> None:
    if not trace_recorder:
        return
    try:
        trace_recorder.add(*args, **kwargs)
    except Exception:
        pass
