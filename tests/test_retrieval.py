import asyncio

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

    routes = [route for route, _ in vector_calls]
    assert routes == ["planned", "sub_question_1", "rewrite_1"]
    assert chunks
    assert trace["query_plan"]["original_question"] == "迟到三个小时扣多少钱"


def test_retrieve_knowledge_skips_vector_routes_when_embedding_backend_fails(monkeypatch):
    keyword_chunk = {
        "id": "kw-1",
        "chunk_id": "1",
        "content": "迟到30分钟以内罚款50元",
        "file_name": "考勤制度.txt",
        "file_id": 1,
        "route": "keyword",
    }
    status_calls = []

    def fake_query_vectors(query, top_k, knowledge_base_id, route):
        raise retrieval.EmbeddingBackendError("向量化接口调用失败（https://embedding.example/v1/embeddings）：429 Too Many Requests")

    def fake_keyword_recall(db, knowledge_base_id, keywords, top_k):
        return [keyword_chunk]

    async def fake_rerank_chunks(question, chunks):
        return chunks, {"status": "done", "items": []}

    def fake_embedding_backend_status():
        status_calls.append(True)
        if len(status_calls) == 1:
            return {"mode": "openai-compatible", "last_error": ""}
        return {"mode": "unavailable", "last_error": "向量化接口调用失败：429 Too Many Requests"}

    monkeypatch.setattr(retrieval, "query_vectors", fake_query_vectors)
    monkeypatch.setattr(retrieval, "keyword_recall", fake_keyword_recall)
    monkeypatch.setattr(retrieval, "rerank_chunks", fake_rerank_chunks)
    monkeypatch.setattr(retrieval, "embedding_backend_status", fake_embedding_backend_status)

    chunks, trace = asyncio.run(
        retrieval.retrieve_knowledge(
            "考勤 迟到 处罚",
            knowledge_base_id=1,
            db=object(),
            query_plan={
                "original_question": "迟到30分钟以内罚款多少钱",
                "simplified_question": "迟到罚款标准",
                "sub_questions": [],
                "rewrites": [],
                "keywords": ["考勤", "迟到"],
                "required_evidence": [],
            },
        )
    )

    assert [chunk["route"] for chunk in chunks] == ["keyword"]
    assert trace["embedding_error"]
    assert trace["embedding"]["mode"] == "unavailable"
    assert [route["count"] for route in trace["routes"]] == [0, 0, 1]
    assert len(status_calls) == 2


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
