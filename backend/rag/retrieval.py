from __future__ import annotations

import asyncio
import heapq
import logging
import math
import re
from typing import Any, Iterator

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from config import RETRIEVAL_ROUTE_TOP_K
from model.models import KnowledgeFile
from rag.llm import call_chat_json, call_router_json
from rag.milvus_client import EmbeddingBackendError, embedding_backend_status, query_vectors
from rag.rerank import (
    chunk_key,
    rerank_chunks,
    select_final_chunks,
    trace_chunk,
)


ROUTE_CONFIDENCE_THRESHOLD = 0.55

# 关键词召回（issue #59）：窗口几何、取数批大小与 SQL 预筛参数。
# 单批行数直接决定召回路径的峰值内存上界（单批行数 × 单文件最大体积），
# 与知识库文件总数无关；同时兼顾小文件书库的往返次数。
KEYWORD_CHUNK_SIZE = 900
KEYWORD_CHUNK_OVERLAP = 180
KEYWORD_RECALL_BATCH_SIZE = 4
LIKE_ESCAPE = "!"
# 与 _keyword_score 一致的高权重场景词。
KEYWORD_BONUS_TERMS = frozenset({"迟到", "早退", "旷工", "罚款", "处罚", "考勤"})
KEYWORD_CLOSE_WINDOW = 120

_WHITESPACE_RE = re.compile(r"\s+")
_DIGIT_RE = re.compile(r"\d")

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
        return _fallback_decision(f"路由模型调用失败，保守进入 RAG：{exc}")


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
    """关键词召回：成本与「命中的内容量」相关，而不是与知识库总量相关（issue #59）。

    旧实现每次提问都把整库文件的 LONGTEXT ``content`` 全量读进内存、逐文件整篇归一化、
    再逐窗口重新归一化，最后只返回 ``top_k`` 条——内存与耗时随知识库总量线性膨胀，而
    ``top_k`` 只截断输出、不截断输入。这里把成本分三层收敛：

    * 取数层 ``_iter_keyword_candidate_files``：SQL 侧先按关键词做 LIKE 预筛，再按主键
      游标分批（带 ``LIMIT``）取数，单批内存有上界且与知识库文件总数无关；
    * 文件层：每个文件只做一次整篇归一化，用它精确判定「该文件是否可能命中」；
    * 窗口层 ``_collect_file_keyword_candidates``：窗口在归一化文本上的区间由空白映射推出，
      再在整篇归一化文本上带区间边界统计命中，不再物化窗口文本、也不逐窗口重新归一化；
      候选只保留分值最高的 ``top_k`` 条（有界堆），不再为每个窗口各留一个字典。

    返回值与旧实现逐条一致（字段、顺序、分值），等价性由
    tests/test_keyword_recall_memory_59.py 中冻结的旧实现做回归基线。
    """
    if top_k <= 0:
        return []
    clean_keywords = _expand_keywords(keywords)
    if not clean_keywords:
        return []
    case_fold = _needs_case_fold(clean_keywords)
    prepared_keywords = _prepare_keywords(clean_keywords, case_fold)
    if not prepared_keywords:
        return []
    best: list[tuple[float, int, dict]] = []
    order = 0
    for file_id, file_name, content in _iter_keyword_candidate_files(
        db, knowledge_base_id, clean_keywords, case_fold
    ):
        order = _collect_file_keyword_candidates(
            file_id, file_name, content, prepared_keywords, case_fold, top_k, best, order
        )
    # 与旧实现 candidates.sort(key=keyword_score, reverse=True)[:top_k] 等价：Python 排序稳定，
    # 同分候选保持「文件主键序、窗口起点序」；这里用 -order 复刻同一顺序。
    return [item[2] for item in sorted(best, key=lambda item: (-item[0], -item[1]))]


def _iter_keyword_candidate_files(
    db: Session, knowledge_base_id: int, clean_keywords: list[str], case_fold: bool = True
) -> Iterator[tuple[int, str, str]]:
    """按主键游标分批产出 (file_id, name, content)，只取「可能命中」的文件。

    两个上界同时生效：SQL 侧 ``LIMIT`` 限制单批行数，``id > last_id`` 只向前扫描（不会
    重复扫已经处理过的行）；预筛推不下去时退化为整库分批扫描，内存上界不变。
    """
    prefilter = _sql_prefilter_patterns(clean_keywords, case_fold)
    last_id = 0
    while True:
        query = db.query(KnowledgeFile.id, KnowledgeFile.name, KnowledgeFile.content).filter(
            KnowledgeFile.knowledge_base_id == knowledge_base_id,
            KnowledgeFile.id > last_id,
        )
        if prefilter is not None:
            # 关键词含字母时先在库侧折叠大小写，再交给 LIKE；纯汉字关键词不必多做一遍。
            column = func.lower(KnowledgeFile.content) if case_fold else KnowledgeFile.content
            query = query.filter(or_(*[column.like(pattern, escape=LIKE_ESCAPE) for pattern in prefilter]))
        rows = query.order_by(KnowledgeFile.id).limit(KEYWORD_RECALL_BATCH_SIZE).all()
        if not rows:
            return
        for file_id, file_name, content in rows:
            yield int(file_id), file_name, content or ""
        last_id = int(rows[-1][0])


def _sql_prefilter_patterns(clean_keywords: list[str], case_fold: bool = True) -> list[str] | None:
    """SQL 侧 LIKE 预筛模式（命中结果的必要条件），None 表示只能整库分批扫描。

    关键词匹配的语义是「去掉空白 + 忽略大小写后的子串匹配」，SQL 里没法逐字复刻。这里退
    一步只要求「字符按序出现」：没有空白的关键词就是普通子串（既必要也充分），含空白的
    关键词用 ``%`` 夹住每个字符（只是必要条件，例如 "迟到 罚款" 允许正文里隔着空白）。
    真正命中的行一定过筛，因此不会漏召回，只可能多取几行；多取的行由 Python 侧的精确
    判定再筛掉。

    含非 ASCII 大小写字母的关键词放弃下推：SQLite 的 ``LOWER``/``LIKE`` 只折叠 ASCII，
    下推可能漏掉正文用另一种大小写写法的命中——宁可慢，不可漏。
    """
    if case_fold and any(not _sql_case_foldable(keyword) for keyword in clean_keywords):
        return None
    patterns = []
    for keyword in clean_keywords:
        normalized = _normalize_for_match(keyword, case_fold)
        if len(normalized) < 2:
            continue
        if _WHITESPACE_RE.search(keyword):
            patterns.append("%" + "%".join(_escape_like_char(char) for char in normalized) + "%")
        else:
            patterns.append("%" + _escape_like_char(normalized) + "%")
    return patterns or None


def _sql_case_foldable(keyword: str) -> bool:
    """关键词能否在 SQL 侧安全比较大小写：ASCII 交给 LOWER/LIKE，无大小写的字符原样比较。"""
    return all(char.isascii() or char.lower() == char.upper() for char in keyword)


def _escape_like_char(char: str) -> str:
    if char == LIKE_ESCAPE or char in {"%", "_"}:
        return LIKE_ESCAPE + char
    return char


def _needs_case_fold(clean_keywords: list[str]) -> bool:
    """关键词里是否含有大小写字母。

    全是汉字/数字/标点时不需要折叠大小写：这类字符的大小写形式就是它自己，正文里能命中
    的位置与折叠后完全相同。省掉整篇 ``str.lower()`` 很值——它对大字符串会临时放大数倍
    内存（实测 200 万字符约 27MB 峰值），而中文语料的提问关键词几乎都不含字母。
    """
    return any(char.lower() != char.upper() for keyword in clean_keywords for char in keyword)


def _prepare_keywords(clean_keywords: list[str], case_fold: bool = True) -> list[tuple[str, str, float]]:
    """(原始关键词, 归一化关键词, 权重)，权重规则与 ``_keyword_score`` 保持一致。"""
    prepared = []
    for keyword in clean_keywords:
        normalized = _normalize_for_match(keyword, case_fold)
        if len(normalized) < 2:
            continue
        weight = 1.0
        if _DIGIT_RE.search(normalized):
            weight += 3.0
        if len(normalized) >= 4:
            weight += 2.0
        if normalized in KEYWORD_BONUS_TERMS:
            weight += 4.0
        prepared.append((keyword, normalized, weight))
    return prepared


def _collect_file_keyword_candidates(
    file_id: int,
    file_name: str,
    content: str,
    prepared_keywords: list[tuple[str, str, float]],
    case_fold: bool,
    top_k: int,
    best: list[tuple[float, int, dict]],
    order: int,
) -> int:
    """把单个文件里命中的窗口并入 top_k 候选（有界堆），返回更新后的候选序号。"""
    normalized_content = _normalize_for_match(content, case_fold)
    occurrences = []
    for keyword, normalized, weight in prepared_keywords:
        if len(normalized) >= 2 and normalized in normalized_content:
            occurrences.append((keyword, normalized, weight))
    if not occurrences:
        return order
    starts = _NormalizedOffsetMap(content, len(normalized_content))
    ends = _NormalizedOffsetMap(content, len(normalized_content))
    for raw_start in _keyword_chunk_starts(len(content)):
        raw_end = min(raw_start + KEYWORD_CHUNK_SIZE, len(content))
        window_start = starts.rank(raw_start)
        window_end = ends.rank(raw_end)
        hits = []
        for keyword, normalized, weight in occurrences:
            # 归一化后的窗口就是归一化全文的一段（去空白是逐字符过滤，前缀仍映射到前缀），
            # 所以直接在全文上带 [window_start, window_end) 边界统计即可，既不用把窗口文本
            # 物化出来，也不用重新归一化。位置用全文坐标：打分只看首次位置的相对间隔与是否
            # 互不相同，整体平移不影响分值。
            count = normalized_content.count(normalized, window_start, window_end)
            if count > 0:
                hits.append(
                    (keyword, normalized, weight, count, normalized_content.find(normalized, window_start, window_end))
                )
        if not hits:
            # 窗口内没有任何关键词出现：旧实现下该窗口得 0 分且命中集为空，必然落选。
            continue
        score, matched_keywords = _score_from_hits(hits)
        if score <= 0 or not matched_keywords:
            continue
        chunk_content = content[raw_start:raw_end].strip()
        if not chunk_content:
            continue
        order += 1
        chunk_id = str(raw_start)
        _keep_best_candidate(
            best,
            top_k,
            order,
            score,
            {
                "id": f"{file_id}_{chunk_id}",
                "chunk_id": chunk_id,
                "content": chunk_content,
                "file_name": file_name,
                "file_id": file_id,
                "route": "keyword",
                "keyword_score": score,
                # 命中证据随候选一起带出，供 select_final_chunks 校验（issue #56）。
                "keyword_hits": len(matched_keywords),
                "matched_keywords": matched_keywords,
            },
        )
    return order


def _keep_best_candidate(
    best: list[tuple[float, int, dict]], top_k: int, order: int, score: float, candidate: dict
) -> None:
    """有界 top_k 堆：只保留分值最高的 top_k 条，同分保留更早出现的候选。

    堆顶是「最差」条目（分值最低；同分时 -order 最小即出现最晚），因此新候选只要比堆顶
    更好就替换它。这样内存只与 ``top_k`` 相关，不再为每个窗口都留一个候选字典。
    """
    entry = (score, -order, candidate)
    if len(best) < top_k:
        heapq.heappush(best, entry)
    elif entry[:2] > best[0][:2]:
        heapq.heapreplace(best, entry)


def rrf_fuse(route_results: list[tuple[str, list[dict]]], k: int = 60) -> list[dict]:
    fused: dict[str, dict] = {}
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
            entry = fused.setdefault(key, {**chunk, "routes": [], "rrf_score": 0.0})
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
    """一段文本的关键词分值（字符串入口，供回归用例与等价性基线使用）。

    keyword_recall 走的是窗口游标版本，但打分规则只有 ``_score_from_hits`` 一份，
    字符串入口与窗口路径共用它。
    """
    normalized_content = _normalize_for_match(content)
    hits = _keyword_hits(normalized_content, _prepare_keywords(keywords))
    return _score_from_hits(hits)[0]


def _matched_query_keywords(content: str, keywords: list[str]) -> list[str]:
    """返回内容里真正命中的查询关键词（保持传入顺序、按归一化形式去重）。

    issue #56：窗口是否与本次提问相关，只由这个命中集合决定；内容自身的场景
    特征词（考勤、迟到、罚款…）不构成相关性证据。
    """
    normalized_content = _normalize_for_match(content)
    hits = _keyword_hits(normalized_content, _prepare_keywords(keywords))
    matched: list[str] = []
    seen: set[str] = set()
    for keyword, normalized, _, count, _ in hits:
        if count <= 0 or normalized in seen:
            continue
        seen.add(normalized)
        matched.append(keyword)
    return matched


def _keyword_hits(
    normalized_content: str, prepared_keywords: list[tuple[str, str, float]]
) -> list[tuple[str, str, float, int, int]]:
    """在已归一化文本上算命中：(原始关键词, 归一化关键词, 权重, 非重叠命中数, 首次出现位置)。"""
    hits = []
    for keyword, normalized, weight in prepared_keywords:
        count = normalized_content.count(normalized)
        if count <= 0:
            continue
        hits.append((keyword, normalized, weight, count, normalized_content.find(normalized)))
    return hits


def _score_from_hits(hits: list[tuple[str, str, float, int, int]]) -> tuple[float, list[str]]:
    """关键词打分的唯一实现，返回 (分值, 命中的查询关键词)。

    hits 里的首次出现位置在「同一坐标系」内比较（字符串入口是整段文本，窗口路径是窗口
    内偏移），因为相邻命中判定用的是相对距离。
    """
    score = 0.0
    matched_positions = []
    matched: list[str] = []
    seen: set[str] = set()
    for keyword, normalized, weight, count, first_position in hits:
        if count <= 0:
            continue
        score += count * weight
        if first_position >= 0:
            matched_positions.append(first_position)
        if normalized not in seen:
            seen.add(normalized)
            matched.append(keyword)
    # 这里不再按内容自身的特征短语（考勤/迟到+早退/30分钟以内/罚款50元…）无条件加分：
    # 那与「本次查询问的是什么」无关，会让整段跑题内容拿到高分并被插到上下文首位。
    # 相关性只由上面的「命中本次查询关键词」决定，零命中内容得 0 分。
    unique_hits = len({pos for pos in matched_positions if pos >= 0})
    score += unique_hits * 1.5
    if _has_close_positions(matched_positions, KEYWORD_CLOSE_WINDOW):
        score += 8.0
    return score, matched


def _has_close_matches(content: str, keywords: list[str], window: int = KEYWORD_CLOSE_WINDOW) -> bool:
    """一段（已归一化的）文本里，不同关键词的首次出现位置是否有 3 个落在同一窗口内。"""
    positions = []
    for keyword in keywords:
        normalized_keyword = _normalize_for_match(keyword)
        if len(normalized_keyword) < 2:
            continue
        pos = content.find(normalized_keyword)
        if pos >= 0:
            positions.append(pos)
    return _has_close_positions(positions, window)


def _has_close_positions(positions: list[int], window: int = KEYWORD_CLOSE_WINDOW) -> bool:
    if len(positions) < 3:
        return False
    positions.sort()
    return any(positions[index + 2] - positions[index] <= window for index in range(len(positions) - 2))


class _NormalizedOffsetMap:
    """原始文本偏移 → 归一化文本偏移（去掉空白后的下标），查询偏移必须单调不减。

    窗口边界在原始文本坐标上（``chunk_id`` 就是原始偏移），而关键词命中位置在归一化文本
    坐标上，两者必须换算。每次只统计「距上次查询新增的那一段」里的非空白字符数，整篇恰好
    被扫一遍，不做逐字符或逐空白游程的 Python 级循环（空白多的文档上差一个数量级）。

    归一化长度与原文长度相同时，文本既没有空白、大小写折叠也没有改变长度，映射就是恒等
    映射，连这一次扫描都省掉——中文语料大多走这条快路径。
    """

    __slots__ = ("_content", "_identity", "_base", "_count")

    def __init__(self, content: str, normalized_length: int):
        self._content = content
        self._identity = normalized_length == len(content)
        self._base = 0  # 已经统计到的原始偏移
        self._count = 0  # content[: self._base] 里的非空白字符数

    def rank(self, offset: int) -> int:
        if self._identity:
            return offset
        if offset > self._base:
            self._count += len(_WHITESPACE_RE.sub("", self._content[self._base : offset]))
            self._base = offset
        return self._count


def _normalize_for_match(text: str, case_fold: bool = True) -> str:
    """去空白 + 转小写；``case_fold=False`` 时省掉整篇 str.lower()（见 _needs_case_fold）。"""
    value = "" if text is None else str(text)
    stripped = _WHITESPACE_RE.sub("", value)
    return stripped.lower() if case_fold else stripped


def _keyword_chunk_starts(
    content_length: int, chunk_size: int = KEYWORD_CHUNK_SIZE, chunk_overlap: int = KEYWORD_CHUNK_OVERLAP
) -> range:
    """窗口起点（原始偏移），步长与旧的切块实现一致。"""
    return range(0, content_length, max(chunk_size - chunk_overlap, 1))


def _split_keyword_chunks(
    content: str, chunk_size: int = KEYWORD_CHUNK_SIZE, chunk_overlap: int = KEYWORD_CHUNK_OVERLAP
) -> list[dict]:
    """旧实现的窗口切分：保留给等价性基线与人工排查，召回路径不再整篇物化窗口。"""
    chunks = []
    for start in _keyword_chunk_starts(len(content), chunk_size, chunk_overlap):
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
