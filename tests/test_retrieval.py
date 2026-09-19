import asyncio
import threading
import time

from rag import retrieval


def test_retrieve_knowledge_uses_sub_questions_and_rewrites(monkeypatch):
    vector_calls = []

    def fake_query_vectors(query, top_k, knowledge_base_id, route):
        vector_calls.append((route, query))
        return [
            {
                "id": route,
                "chunk_id": route,
                "content": query,
                "file_name": "制度.txt",
                "file_id": len(vector_calls),
                "route": route,
            }
        ]

    def fake_keyword_recall(db, knowledge_base_id, keywords, top_k):
        return []

    async def fake_rerank_chunks(question, chunks):
        return chunks, {"status": "done", "items": []}

    monkeypatch.setattr(retrieval, "query_vectors", fake_query_vectors)
    monkeypatch.setattr(retrieval, "keyword_recall", fake_keyword_recall)
    monkeypatch.setattr(retrieval, "rerank_chunks", fake_rerank_chunks)

    chunks, trace = asyncio.run(
        retrieval.retrieve_knowledge(
            "考勤 迟到 旷工 处罚",
            knowledge_base_id=1,
            db=object(),
            query_plan={
                "original_question": "迟到三个小时扣多少钱",
                "simplified_question": "考勤 迟到 旷工 处罚",
                "sub_questions": ["迟到超过30分钟如何认定"],
                "rewrites": ["迟到三个小时怎么处理"],
                "keywords": ["考勤", "旷工"],
                "required_evidence": ["迟到认定规则"],
            },
        )
    )

    # 多路召回并发执行后调用发生的先后不再有保证，trace 中的顺序才是对外语义
    # （它按 route_specs 的生成顺序固定），这里同样能验证“采纳子问题/改写 + 同义 query 去重”。
    assert {route for route, _ in vector_calls} == {"planned", "sub_question_1", "rewrite_1"}
    vector_routes_in_trace = [item["route"] for item in trace["routes"] if item["route"] != "keyword"]
    assert vector_routes_in_trace == ["planned", "sub_question_1", "rewrite_1"]
    assert chunks
    assert trace["query_plan"]["original_question"] == "迟到三个小时扣多少钱"


def test_retrieve_knowledge_keeps_event_loop_responsive_and_recalls_concurrently(monkeypatch):
    """一轮多路检索期间事件循环必须仍在被调度，且各路召回是并发而不是串行等待。"""
    route_count = 8
    route_delay = 0.2
    lock = threading.Lock()
    inflight = {"now": 0, "max": 0}
    calls = []

    def fake_query_vectors(query, top_k, knowledge_base_id, route):
        with lock:
            calls.append(route)
            inflight["now"] += 1
            inflight["max"] = max(inflight["max"], inflight["now"])
        try:
            time.sleep(route_delay)
            return []
        finally:
            with lock:
                inflight["now"] -= 1

    def fake_keyword_recall(db, knowledge_base_id, keywords, top_k):
        return []

    async def fake_rerank_chunks(question, chunks):
        return chunks, {"status": "done", "items": []}

    monkeypatch.setattr(retrieval, "query_vectors", fake_query_vectors)
    monkeypatch.setattr(retrieval, "keyword_recall", fake_keyword_recall)
    monkeypatch.setattr(retrieval, "rerank_chunks", fake_rerank_chunks)

    async def scenario():
        heartbeats = []

        async def ticker():
            while True:
                await asyncio.sleep(0.02)
                heartbeats.append(time.perf_counter())

        ticker_task = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)  # 先让心跳协程真正跑起来
        started_at = time.perf_counter()
        _, trace = await retrieval.retrieve_knowledge(
            "迟到怎么处理",
            knowledge_base_id=1,
            db=object(),
            query_plan={
                "original_question": "迟到怎么处理",
                "simplified_question": "考勤 迟到 处理",
                "sub_questions": ["迟到多久算旷工", "迟到罚款多少"],
                "hyde_document": "员工迟到30分钟以内罚款50元。",
                "rewrites": ["迟到怎么罚", "上班迟到扣多少钱", "迟到处罚规定"],
                "keywords": ["迟到"],
            },
        )
        finished_at = time.perf_counter()
        ticker_task.cancel()
        during = [beat for beat in heartbeats if started_at < beat < finished_at]
        return during, finished_at - started_at, trace

    heartbeats_during_retrieval, elapsed, trace = asyncio.run(scenario())

    assert [item["route"] for item in trace["routes"]] == [
        "planned",
        "simplified",
        "sub_question_1",
        "sub_question_2",
        "hyde",
        "rewrite_1",
        "rewrite_2",
        "rewrite_3",
        "keyword",
    ]
    assert len(calls) == route_count
    # 检索期间事件循环仍在调度心跳协程：修复前这里是 0（同步调用独占事件循环）。
    assert heartbeats_during_retrieval
    # 多路召回并发：任一时刻都有多路在途，而不是一路接一路。
    assert inflight["max"] >= 2
    # 8 路各 0.2s：串行需要 1.6s，并发后应明显低于串行耗时。
    assert elapsed < route_count * route_delay * 0.6


def test_build_route_specs_does_not_reintroduce_empty_question():
    assert retrieval._build_route_specs("   ", {}) == []


def test_build_route_specs_preserves_falsy_query_values():
    assert retrieval._build_route_specs(0, {"simplified_question": False}) == [
        ("planned", "0"),
        ("simplified", "False"),
    ]


def test_route_decision_treats_nan_confidence_as_fallback():
    decision = retrieval._normalize_decision(
        {
            "need_rag": False,
            "confidence": "nan",
            "reason": "bad confidence",
        }
    )

    assert decision["route"] == "rag"
    assert decision["source"] == "fallback"


def test_clip_preserves_falsy_values():
    assert retrieval._clip(0, 10) == "0"
    assert retrieval._clip(False, 10) == "False"
    assert retrieval._clip(None, 10) == ""


def test_normalize_for_match_preserves_falsy_values():
    assert retrieval._normalize_for_match(0) == "0"
    assert retrieval._normalize_for_match(False) == "false"
    assert retrieval._normalize_for_match(None) == ""


def test_keyword_helpers_only_treat_none_as_empty_text():
    assert retrieval._dedupe_keywords(["迟到", False, 0, None]) == ["迟到", "False"]
    assert retrieval._expand_keywords([False, 0, None]) == ["False"]
    assert retrieval._fallback_keywords(False) == ["False"]


def test_retrieve_knowledge_empty_question_does_not_add_empty_keyword_route(monkeypatch):
    vector_calls = []
    keyword_calls = []

    def fake_query_vectors(query, top_k, knowledge_base_id, route):
        vector_calls.append((route, query))
        return []

    def fake_keyword_recall(db, knowledge_base_id, keywords, top_k):
        keyword_calls.append(keywords)
        return []

    async def fake_rerank_chunks(question, chunks):
        return [], {"status": "skipped", "items": []}

    monkeypatch.setattr(retrieval, "query_vectors", fake_query_vectors)
    monkeypatch.setattr(retrieval, "keyword_recall", fake_keyword_recall)
    monkeypatch.setattr(retrieval, "rerank_chunks", fake_rerank_chunks)

    chunks, trace = asyncio.run(
        retrieval.retrieve_knowledge(
            "   ",
            knowledge_base_id=1,
            db=object(),
            query_plan={"keywords": [], "required_evidence": []},
        )
    )

    assert chunks == []
    assert trace["routes"] == []
    assert vector_calls == []
    assert keyword_calls == []


def test_rrf_fuse_skips_non_list_route_results():
    fused = retrieval.rrf_fuse([
        ("vector", None),
        ("keyword", [{"file_id": 1, "chunk_id": "a", "content": "hit"}]),
    ])

    assert len(fused) == 1
    assert fused[0]["chunk_id"] == "a"
    assert fused[0]["routes"] == [{"route": "keyword", "rank": 1}]


def test_rrf_fuse_skips_malformed_route_entries():
    fused = retrieval.rrf_fuse([
        None,
        ("too-short",),
        ("keyword", [{"file_id": 1, "chunk_id": "a", "content": "hit"}]),
    ])

    assert len(fused) == 1
    assert fused[0]["chunk_id"] == "a"


def test_rrf_fuse_skips_non_dict_chunks():
    fused = retrieval.rrf_fuse([
        ("keyword", [None, "bad", {"file_id": 1, "chunk_id": "a", "content": "hit"}]),
    ])

    assert len(fused) == 1
    assert fused[0]["chunk_id"] == "a"
