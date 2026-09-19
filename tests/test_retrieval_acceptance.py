"""Retrieval acceptance goal (docs/MAINTENANCE_GOAL_CLOSURE.md, goal 3).

tests/test_retrieval.py and tests/test_rerank.py cover the individual helpers. This
file drives the whole chain of ``retrieve_knowledge`` -- multi-route recall, RRF fusion,
rerank and final context selection -- with the real fusion/truncation code and only the
provider boundary stubbed:

* marginal recall (``query_vectors`` / ``keyword_recall``) is replaced, because it is
  asserted separately against a real Milvus Lite store in test_milvus_acceptance.py;
* the rerank HTTP call is replaced by an in-process transport, while the real
  ``rerank_chunks`` / ``select_final_chunks`` logic under test stays untouched.

No network access is required.
"""

import asyncio

import httpx
import pytest

from config import RETRIEVAL_RERANK_TOP_N, RETRIEVAL_ROUTE_TOP_K
from rag import rerank, retrieval

ROUTE_LABELS = [
    "planned",
    "simplified",
    "sub_question_1",
    "sub_question_2",
    "hyde",
    "rewrite_1",
    "rewrite_2",
]


def _chunk(key, content):
    return {
        "id": key,
        "chunk_id": key,
        "content": content,
        "file_name": "考勤制度.txt",
        "file_id": 1,
        "route": "vector",
    }


C1 = _chunk("c1", "迟到一次罚款50元")
C2 = _chunk("c2", "迟到超过30分钟视为旷工半天")
C3 = _chunk("c3", "当月迟到三次以上加倍处罚")
C4 = _chunk("c4", "迟到三个小时按旷工一天处理")
C5 = _chunk("c5", "请假需要提前一天申请")

QUERY_PLAN = {
    "original_question": "迟到三个小时怎么处理",
    "simplified_question": "考勤 迟到 处理",
    "sub_questions": ["迟到超过30分钟如何认定", "迟到三小时是否算旷工"],
    "hyde_document": "员工迟到超过30分钟按旷工处理，并处以罚款。",
    "rewrites": ["迟到扣多少钱", "迟到处罚标准"],
    "keywords": ["迟到", "旷工"],
    "required_evidence": ["迟到认定规则"],
}

# Fusion scores (k=60, rank counts from 1) are all distinct, so the expected fused
# order c4 > c5 > c3 > c1 > c2 is deterministic rather than a float coincidence.
ROUTE_RESULTS = {
    "planned": [C1, C2, C3],
    "simplified": [C3],
    "sub_question_1": [C4],
    "sub_question_2": [C4],
    "hyde": [C5, C4],
    "rewrite_1": [C5],
    "rewrite_2": [],
}
FUSED_ORDER = ["c4", "c5", "c3", "c1", "c2"]

# Deliberately different from the fused order: the final context must follow the rerank
# scores, so a chain that silently skipped or ignored rerank would fail these tests.
RERANK_SCORES = {
    C1["content"]: 0.4,
    C2["content"]: 0.95,
    C3["content"]: 0.35,
    C4["content"]: 0.2,
    C5["content"]: 0.1,
}
RERANKED_ORDER = ["c2", "c1", "c3", "c4", "c5"]


class _Recorder:
    def __init__(self):
        self.events = []

    def add(self, event, *args, **payload):
        self.events.append({"event": event, "args": args, **payload})


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _install_rerank_transport(monkeypatch, handler):
    """Route the rerank HTTP call to ``handler(request)``; returns captured requests."""
    captured = []

    class _Client:
        def __init__(self, *args, **kwargs):
            captured.append({"args": args, "timeout": kwargs.get("timeout")})

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, **kwargs):
            request = captured[-1]
            request.update({"url": url, "json": kwargs.get("json"), "headers": kwargs.get("headers")})
            return _FakeResponse(handler(request))

    monkeypatch.setattr(rerank, "RERANK_API_KEY", "test-rerank-key")
    monkeypatch.setattr(rerank.httpx, "AsyncClient", _Client)
    return captured


def _score_by_content(scores):
    def handler(request):
        documents = request["json"]["documents"]
        return {
            "output": {
                "results": [
                    {"index": index, "relevance_score": scores[document]}
                    for index, document in enumerate(documents)
                ]
            }
        }

    return handler


def _raising_handler(exc):
    def handler(_request):
        raise exc

    return handler


def _install_recall(monkeypatch, route_results, keyword_chunks=()):
    """Replace marginal recall; records every recall call in order."""
    calls = []

    def fake_query_vectors(query, top_k, knowledge_base_id, route):
        calls.append({"route": route, "query": query, "top_k": top_k, "knowledge_base_id": knowledge_base_id})
        return [dict(chunk) for chunk in route_results.get(route, [])]

    def fake_keyword_recall(db, knowledge_base_id, keywords, top_k):
        calls.append(
            {"route": "keyword", "query": list(keywords), "top_k": top_k, "knowledge_base_id": knowledge_base_id}
        )
        return [dict(chunk) for chunk in keyword_chunks]

    monkeypatch.setattr(retrieval, "query_vectors", fake_query_vectors)
    monkeypatch.setattr(retrieval, "keyword_recall", fake_keyword_recall)
    return calls


def _retrieve(knowledge_base_id=7, trace_recorder=None, question="迟到三个小时怎么处理", plan=None):
    return asyncio.run(
        retrieval.retrieve_knowledge(
            question,
            knowledge_base_id=knowledge_base_id,
            db=object(),
            trace_recorder=trace_recorder,
            query_plan=dict(QUERY_PLAN if plan is None else plan),
        )
    )


def test_acceptance_multi_route_rrf_rerank_final_order(monkeypatch):
    calls = _install_recall(monkeypatch, ROUTE_RESULTS)
    captured = _install_rerank_transport(monkeypatch, _score_by_content(RERANK_SCORES))

    final, trace = _retrieve()

    assert [call["route"] for call in calls] == [*ROUTE_LABELS, "keyword"]
    assert all(call["top_k"] == RETRIEVAL_ROUTE_TOP_K for call in calls)
    assert all(call["knowledge_base_id"] == 7 for call in calls)

    assert [item["chunk_id"] for item in trace["rrf"]] == FUSED_ORDER
    assert trace["rrf"][0]["rrf_score"] == pytest.approx(2 / 61 + 1 / 62)
    assert trace["rrf"][0]["routes"] == [
        {"route": "sub_question_1", "rank": 1},
        {"route": "sub_question_2", "rank": 1},
        {"route": "hyde", "rank": 2},
    ]
    assert {item["route"]: item["count"] for item in trace["routes"]} == {
        "planned": 3,
        "simplified": 1,
        "sub_question_1": 1,
        "sub_question_2": 1,
        "hyde": 2,
        "rewrite_1": 1,
        "rewrite_2": 0,
        "keyword": 0,
    }

    # The provider received the fused candidates in fused order, once, with the plan's
    # original question and the configured top_n.
    assert len(captured) == 1
    request = captured[0]
    assert request["url"] == rerank.RERANK_BASE_URL
    assert request["timeout"] == rerank.RERANK_TIMEOUT_SECONDS
    assert request["headers"]["Authorization"] == "Bearer test-rerank-key"
    assert request["json"]["model"] == rerank.RERANK_MODEL
    assert request["json"]["query"] == QUERY_PLAN["original_question"]
    assert request["json"]["documents"] == [item["excerpt"] for item in trace["rrf"]]
    assert request["json"]["top_n"] == min(RETRIEVAL_RERANK_TOP_N, len(FUSED_ORDER))

    # Final context follows the rerank scores, not the fused order.
    assert [item["chunk_id"] for item in final] == RERANKED_ORDER
    assert final[0]["rerank_score"] == pytest.approx(0.95)
    assert final[0]["rerank_reason"] == "qwen3-rerank relevance score"
    assert trace["rerank"]["status"] == "done"
    assert trace["rerank"]["provider"] == rerank.RERANK_PROVIDER
    assert trace["rerank"]["model"] == rerank.RERANK_MODEL


def test_acceptance_rerank_failure_falls_back_to_fused_order(monkeypatch):
    _install_recall(monkeypatch, ROUTE_RESULTS)
    monkeypatch.setattr(rerank, "RERANK_LLM_FALLBACK_ENABLED", False)
    captured = _install_rerank_transport(monkeypatch, _raising_handler(httpx.ConnectError("connection refused")))

    final, trace = _retrieve()

    assert [item["chunk_id"] for item in final] == FUSED_ORDER
    assert all("rerank_score" not in item for item in final)
    assert len(captured) == 1
    assert trace["rerank"]["status"] == "failed"
    assert trace["rerank"]["provider"] == rerank.RERANK_PROVIDER
    assert "connection refused" in trace["rerank"]["error"]


def test_acceptance_rerank_falls_back_to_llm_with_real_fused_candidates(monkeypatch):
    _install_recall(monkeypatch, ROUTE_RESULTS)
    prompts = {}

    async def fake_call_chat_json(system_prompt, user_prompt, **kwargs):
        prompts["system"] = system_prompt
        prompts["user"] = user_prompt
        return {
            "results": [
                {"id": 1, "score": 0.9, "reason": "直接命中迟到处理规则"},
                {"id": 2, "score": 0.2, "reason": "仅部分相关"},
            ]
        }

    monkeypatch.setattr(rerank, "RERANK_LLM_FALLBACK_ENABLED", True)
    monkeypatch.setattr(rerank, "call_chat_json", fake_call_chat_json)
    _install_rerank_transport(monkeypatch, _raising_handler(httpx.ConnectError("qwen3 unreachable")))

    final, trace = _retrieve()

    # Explicit candidate ids are 1-based over the fused list.
    assert [item["chunk_id"] for item in final] == ["c4", "c5"]
    assert final[0]["rerank_score"] == pytest.approx(0.9)
    assert trace["rerank"]["provider"] == "deepseek_fallback"
    assert trace["rerank"]["fallback_from"] == rerank.RERANK_MODEL
    assert "qwen3 unreachable" in trace["rerank"]["fallback_reason"]
    for key in FUSED_ORDER:
        assert next(chunk["content"] for chunk in ROUTE_RESULTS["planned"] + ROUTE_RESULTS["hyde"] if chunk["chunk_id"] == key) in prompts["user"]


def test_acceptance_keyword_boost_prepends_best_keyword_chunk(monkeypatch):
    keyword_chunk = {**_chunk("k1", "员工迟到每月累计三次以上，公司可以解除劳动合同"), "keyword_score": 12.0}
    _install_recall(monkeypatch, ROUTE_RESULTS, keyword_chunks=[keyword_chunk])
    _install_rerank_transport(
        monkeypatch, _score_by_content({**RERANK_SCORES, keyword_chunk["content"]: 0.05})
    )

    final, trace = _retrieve()

    assert final[0]["chunk_id"] == "k1"
    assert final[0]["keyword_score"] == pytest.approx(12.0)
    assert [item["chunk_id"] for item in final[1:]] == RERANKED_ORDER[: RETRIEVAL_RERANK_TOP_N - 1]
    assert len(final) == RETRIEVAL_RERANK_TOP_N
    assert trace["routes"][-1]["route"] == "keyword"
    assert trace["routes"][-1]["count"] == 1


def test_acceptance_keyword_boost_does_not_duplicate_selected_chunk(monkeypatch):
    duplicate = {**C2, "keyword_score": 12.0, "route": "keyword"}
    _install_recall(monkeypatch, ROUTE_RESULTS, keyword_chunks=[duplicate])
    _install_rerank_transport(monkeypatch, _score_by_content(RERANK_SCORES))

    final, _ = _retrieve()

    assert [item["chunk_id"] for item in final] == RERANKED_ORDER
    assert len(final) == RETRIEVAL_RERANK_TOP_N
    keys = [rerank.chunk_key(item) for item in final]
    assert len(keys) == len(set(keys))


def test_acceptance_empty_recall_skips_rerank_without_provider_call(monkeypatch):
    calls = _install_recall(monkeypatch, {})
    captured = _install_rerank_transport(monkeypatch, _score_by_content({}))

    final, trace = _retrieve()

    assert final == []
    assert trace["rrf"] == []
    assert trace["rerank"] == {"status": "skipped", "items": []}
    assert [call["route"] for call in calls] == [*ROUTE_LABELS, "keyword"]
    assert [item["count"] for item in trace["routes"]] == [0] * len(ROUTE_LABELS + ["keyword"])
    assert captured == []


def test_acceptance_partial_empty_routes_still_produce_context(monkeypatch):
    _install_recall(monkeypatch, {"simplified": [C1]})
    captured = _install_rerank_transport(monkeypatch, _score_by_content({C1["content"]: 0.7}))

    final, trace = _retrieve()

    assert {item["route"]: item["count"] for item in trace["routes"]}["planned"] == 0
    assert [item["chunk_id"] for item in trace["rrf"]] == ["c1"]
    assert [item["chunk_id"] for item in final] == ["c1"]
    assert captured[0]["json"]["documents"] == [C1["content"]]
    assert trace["rerank"]["status"] == "done"


def test_acceptance_single_route_failure_is_not_swallowed(monkeypatch):
    recorder = _Recorder()

    def fake_query_vectors(query, top_k, knowledge_base_id, route):
        if route == "hyde":
            raise RuntimeError("milvus search failed")
        return [dict(chunk) for chunk in ROUTE_RESULTS.get(route, [])]

    monkeypatch.setattr(retrieval, "query_vectors", fake_query_vectors)
    monkeypatch.setattr(retrieval, "keyword_recall", lambda *args, **kwargs: [])

    with pytest.raises(RuntimeError, match="milvus search failed"):
        _retrieve(trace_recorder=recorder)

    # The failure aborts the retrieval instead of quietly returning a partial context.
    assert [event["event"] for event in recorder.events] == ["retriever_started"]


def test_acceptance_route_plan_is_deduplicated_and_capped(monkeypatch):
    plan = {
        "original_question": "迟到处理",
        "simplified_question": "迟到 处理",
        "sub_questions": [
            "迟到 处理",
            "迟到超过30分钟如何认定",
            "迟到三小时是否算旷工",
            "多余的子问题应被截断",
        ],
        "hyde_document": "迟到处理的相关制度说明",
        "rewrites": ["迟到扣多少钱", "迟到怎么处罚", "迟到申诉流程", "多余的改写应被截断"],
        "keywords": ["迟到"],
    }
    calls = _install_recall(monkeypatch, {})
    _install_rerank_transport(monkeypatch, _score_by_content({}))

    _retrieve(plan=plan)

    assert [call["route"] for call in calls] == [
        "planned",
        "simplified",
        "sub_question_2",
        "sub_question_3",
        "hyde",
        "rewrite_1",
        "rewrite_2",
        "rewrite_3",
        "keyword",
    ]


def test_acceptance_rerank_candidate_window_is_capped(monkeypatch):
    many = [_chunk(f"m{index}", f"候选段落{index}：员工迟到处理说明") for index in range(14)]
    _install_recall(monkeypatch, {"planned": many})
    captured = _install_rerank_transport(
        monkeypatch, _score_by_content({chunk["content"]: 0.5 for chunk in many})
    )

    final, trace = _retrieve()

    documents = captured[0]["json"]["documents"]
    assert len(documents) == 12
    assert documents == [chunk["content"] for chunk in many[:12]]
    assert captured[0]["json"]["top_n"] == RETRIEVAL_RERANK_TOP_N
    assert [item["chunk_id"] for item in trace["rrf"]] == [f"m{index}" for index in range(10)]
    assert [item["chunk_id"] for item in final] == [f"m{index}" for index in range(RETRIEVAL_RERANK_TOP_N)]
