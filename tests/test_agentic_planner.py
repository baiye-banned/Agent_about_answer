import asyncio
import json

from agent import agent
from tool import tools


def test_normalize_plan_builds_structured_queries():
    plan = agent._normalize_plan(
        {
            "simplified_question": "考勤 迟到 旷工 处罚",
            "sub_questions": ["迟到超过30分钟如何认定", "旷工如何处罚"],
            "rewrites": ["迟到三个小时怎么处理"],
            "keywords": ["考勤", "旷工", "处罚"],
            "required_evidence": ["迟到认定规则", "罚款规则"],
            "max_rounds": 2,
        },
        "迟到三个小时扣多少钱",
    )

    assert plan["original_question"] == "迟到三个小时扣多少钱"
    assert plan["simplified_question"] == "考勤 迟到 旷工 处罚"
    assert plan["queries"][0] == "考勤 迟到 旷工 处罚"
    assert "迟到超过30分钟如何认定" in plan["queries"]
    assert plan["max_rounds"] == 2


def test_agentic_retrieve_uses_controlled_plan_before_retrieval(monkeypatch):
    captured = {}

    async def fake_call_chat_json(*args, **kwargs):
        return {
            "simplified_question": "考勤 迟到 旷工 处罚",
            "sub_questions": ["迟到超过30分钟如何认定"],
            "rewrites": ["迟到三个小时怎么处理"],
            "keywords": ["考勤", "旷工", "处罚"],
            "required_evidence": ["迟到认定规则"],
            "max_rounds": 1,
            "reason": "需要先检索考勤制度",
        }

    async def fake_retrieve_knowledge(question, knowledge_base_id, db, trace_recorder=None, query_plan=None):
        captured["question"] = question
        captured["query_plan"] = query_plan
        return (
            [
                {
                    "id": "1",
                    "chunk_id": "1",
                    "content": "30分钟以上视为旷工。",
                    "file_name": "制度.txt",
                    "file_id": 1,
                    "rerank_score": 0.9,
                }
            ],
            {
                "routes": [{"route": "planned"}],
                "rerank": {"status": "done", "items": [{"rerank_score": 0.9}]},
            },
        )

    monkeypatch.setattr(agent, "AGENT_PLANNER_MODE", "controlled")
    monkeypatch.setattr(agent, "call_chat_json", fake_call_chat_json)
    monkeypatch.setattr(agent, "retrieve_knowledge", fake_retrieve_knowledge)

    chunks, trace = asyncio.run(agent.agentic_retrieve_knowledge("迟到三个小时扣多少钱", 1, object()))

    assert chunks
    assert captured["question"] == "考勤 迟到 旷工 处罚"
    assert captured["query_plan"]["original_question"] == "迟到三个小时扣多少钱"
    assert captured["query_plan"]["sub_questions"] == ["迟到超过30分钟如何认定"]
    assert trace["agent"]["planner_mode"] == "controlled"


def test_agentic_retrieve_falls_back_to_memory_aware_question(monkeypatch):
    captured = {}

    async def fake_call_chat_json(*args, **kwargs):
        raise RuntimeError("planner unavailable")

    async def fake_retrieve_knowledge(question, knowledge_base_id, db, trace_recorder=None, query_plan=None):
        captured["question"] = question
        captured["query_plan"] = query_plan
        return [], {"routes": [], "rerank": {"status": "skipped", "items": []}}

    monkeypatch.setattr(agent, "AGENT_PLANNER_MODE", "controlled")
    monkeypatch.setattr(agent, "call_chat_json", fake_call_chat_json)
    monkeypatch.setattr(agent, "retrieve_knowledge", fake_retrieve_knowledge)

    chunks, trace = asyncio.run(agent.agentic_retrieve_knowledge("上一条制度怎么处罚", 1, object()))

    assert chunks == []
    assert captured["question"] == "上一条制度怎么处罚"
    assert captured["query_plan"]["source"] == "controlled_fallback"
    assert trace["agent"]["planner"]["error"] == "planner unavailable"


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

    monkeypatch.setattr(tools, "query_vectors", fake_query_vectors)
    monkeypatch.setattr(tools, "keyword_recall", fake_keyword_recall)
    monkeypatch.setattr(tools, "rerank_chunks", fake_rerank_chunks)

    chunks, trace = asyncio.run(
        tools.retrieve_knowledge(
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


def test_build_route_specs_does_not_reintroduce_empty_question():
    assert tools._build_route_specs("   ", {}) == []


def test_build_route_specs_preserves_falsy_query_values():
    assert tools._build_route_specs(0, {"simplified_question": False}) == [
        ("planned", "0"),
        ("simplified", "False"),
    ]


def test_quality_score_ignores_invalid_rerank_scores():
    score = agent._quality_score(
        [{"content": "valid chunk", "rerank_score": "not-a-number"}],
        {
            "routes": [{"route": "vector"}],
            "rerank": {"status": "done", "items": [{"rerank_score": "bad-score"}]},
        },
    )

    assert score == 0.342


def test_quality_score_treats_invalid_trace_as_empty_trace():
    score = agent._quality_score([{"content": "valid chunk"}], None)

    assert score == 0.175


def test_route_decision_treats_nan_confidence_as_fallback():
    decision = tools._normalize_decision(
        {
            "need_rag": False,
            "confidence": "nan",
            "reason": "bad confidence",
        }
    )

    assert decision["route"] == "rag"
    assert decision["source"] == "fallback"


def test_clip_preserves_falsy_values():
    assert tools._clip(0, 10) == "0"
    assert tools._clip(False, 10) == "False"
    assert tools._clip(None, 10) == ""


def test_normalize_for_match_preserves_falsy_values():
    assert tools._normalize_for_match(0) == "0"
    assert tools._normalize_for_match(False) == "false"
    assert tools._normalize_for_match(None) == ""


def test_keyword_helpers_only_treat_none_as_empty_text():
    assert tools._dedupe_keywords(["迟到", False, 0, None]) == ["迟到", "False"]
    assert tools._expand_keywords([False, 0, None]) == ["False"]
    assert tools._fallback_keywords(False) == ["False"]


def test_agent_query_normalization_preserves_falsy_values():
    assert agent._normalize_queries([0, False, "", None], "fallback") == ["0", "False"]
    assert agent._dedupe_texts([0, False, "", None]) == ["0", "False"]


def test_normalize_plan_ignores_none_list_items():
    plan = agent._normalize_plan(
        {
            "sub_questions": [None, "rule"],
            "keywords": [None, "late"],
        },
        "fallback",
    )

    assert plan["sub_questions"] == ["rule"]
    assert plan["keywords"] == ["late"]
    assert "None" not in plan["queries"]


def test_query_plan_for_round_ignores_non_list_queries():
    plan = agent._query_plan_for_round({"queries": "abc"}, "current", 1)

    assert plan["queries"] == ["current"]


def test_last_message_content_preserves_falsy_values():
    assert agent._last_message_content({"messages": [{"content": 0}]}) == "0"
    assert agent._last_message_content({"messages": [{"content": False}]}) == "False"
    assert agent._last_message_content({"messages": [{"content": None}]}) == ""


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

    monkeypatch.setattr(tools, "query_vectors", fake_query_vectors)
    monkeypatch.setattr(tools, "keyword_recall", fake_keyword_recall)
    monkeypatch.setattr(tools, "rerank_chunks", fake_rerank_chunks)

    chunks, trace = asyncio.run(
        tools.retrieve_knowledge(
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
    fused = tools.rrf_fuse([
        ("vector", None),
        ("keyword", [{"file_id": 1, "chunk_id": "a", "content": "hit"}]),
    ])

    assert len(fused) == 1
    assert fused[0]["chunk_id"] == "a"
    assert fused[0]["routes"] == [{"route": "keyword", "rank": 1}]


def test_rrf_fuse_skips_malformed_route_entries():
    fused = tools.rrf_fuse([
        None,
        ("too-short",),
        ("keyword", [{"file_id": 1, "chunk_id": "a", "content": "hit"}]),
    ])

    assert len(fused) == 1
    assert fused[0]["chunk_id"] == "a"


def test_rrf_fuse_skips_non_dict_chunks():
    fused = tools.rrf_fuse([
        ("keyword", [None, "bad", {"file_id": 1, "chunk_id": "a", "content": "hit"}]),
    ])

    assert len(fused) == 1
    assert fused[0]["chunk_id"] == "a"


def test_rrf_fuse_tool_returns_error_json_for_invalid_input():
    result = json.loads(tools.rrf_fuse_tool.func("{bad-json"))

    assert result["chunks"] == []
    assert "error" in result
