"""issue #55：关键字窗口与入库切片是两套 chunk_id 命名空间，融合时不得互相吞并。

两套分块方案各自从 0 开始编号——入库切片是序号（``chunk_text``），关键字窗口是字符
偏移（``_split_keyword_chunks``）——所以同一个文件里 offset=0 的关键字窗口和序号 0 的
入库切片会算出同一个 ``file_id:chunk_id``。融合阶段按这个键去重，后到的那条被
``setdefault`` 静默吞掉，关键字命中连内容与 keyword_score 一起消失。

这里同时钉住两侧契约：命名空间隔离之后关键字命中必须可见；而「同一段文本」仍然只能
进上下文一次——多路向量召回命中的同一条切片、以及不同分块方案产出的同文片段，都算同
一条上下文。
"""

import pytest

from crud.knowledge_file import chunk_text
from rag import rerank, retrieval


FILE_ID = 7

SEMANTIC_CONTENT = "第一条 目的 为规范公司考勤管理，保障员工权益，特制定本制度。"
KEYWORD_CONTENT = "报销申请需提交原始发票，由财务部在五个工作日内完成审核并付款。"


def _semantic_chunk(chunk_id: str, content: str, *, file_id: int = FILE_ID) -> dict:
    """入库切片候选：``chunk_id`` 是 ``chunk_text`` 的顺序序号。"""
    return {
        "id": f"{file_id}_{chunk_id}",
        "chunk_id": chunk_id,
        "file_id": file_id,
        "file_name": "员工手册.md",
        "content": content,
        "route": "planned",
    }


def _keyword_chunk(chunk_id: str, content: str, *, file_id: int = FILE_ID, **extra) -> dict:
    """关键字窗口候选：``chunk_id`` 是 ``_split_keyword_chunks`` 的字符偏移。"""
    chunk = {
        "id": f"{file_id}_{chunk_id}",
        "chunk_id": chunk_id,
        "file_id": file_id,
        "file_name": "员工手册.md",
        "content": content,
        "route": "keyword",
        "keyword_score": 26.0,
        "keyword_hits": 2,
    }
    chunk.update(extra)
    return chunk


def test_both_chunkers_really_do_number_from_zero():
    """先证红：同一份文档，两套分块方案都从 0 开始编号，冲突是真实存在的。"""
    document = (SEMANTIC_CONTENT + "\n\n" + KEYWORD_CONTENT + "\n") * 12

    semantic_ids = [chunk["id"] for chunk in chunk_text(document, FILE_ID)]
    window_ids = [chunk["chunk_id"] for chunk in retrieval._split_keyword_chunks(document)]

    assert semantic_ids[0] == "0"
    assert window_ids[0] == "0"


def test_chunk_key_separates_the_two_chunking_schemes():
    semantic = _semantic_chunk("0", SEMANTIC_CONTENT)
    keyword = _keyword_chunk("0", KEYWORD_CONTENT)

    assert rerank.chunk_key(semantic) != rerank.chunk_key(keyword)


def test_rrf_fuse_keeps_keyword_window_that_shares_an_id_with_a_semantic_chunk():
    """验收 1：融合后两条都保留，关键字候选的内容与 keyword_score 不被丢弃。"""
    semantic = _semantic_chunk("0", SEMANTIC_CONTENT)
    keyword = _keyword_chunk("0", KEYWORD_CONTENT)

    fused = retrieval.rrf_fuse([("planned", [semantic]), ("keyword", [keyword])])

    assert len(fused) == 2
    assert {chunk["content"] for chunk in fused} == {SEMANTIC_CONTENT, KEYWORD_CONTENT}

    keyword_entry = next(chunk for chunk in fused if chunk["route"] == "keyword")
    assert keyword_entry["keyword_score"] == 26.0
    assert keyword_entry["chunk_id"] == "0"
    assert [entry["route"] for entry in keyword_entry["routes"]] == ["keyword"]


def test_select_final_chunks_injects_keyword_window_that_shares_an_id_with_a_semantic_chunk():
    """验收 2：这条关键字命中必须能进最终上下文（修复前会被判成「已在结果里」）。"""
    semantic = _semantic_chunk("0", SEMANTIC_CONTENT)
    keyword = _keyword_chunk("0", KEYWORD_CONTENT)

    final = rerank.select_final_chunks([semantic], [keyword])

    assert [chunk["content"] for chunk in final] == [KEYWORD_CONTENT, SEMANTIC_CONTENT]
    assert sum(1 for chunk in final if chunk["route"] == "keyword") == 1


def test_non_zero_offset_keyword_window_behaves_as_before():
    """验收 3：offset 非 0 的关键字候选（本来就撞不上键）行为不变。"""
    semantic = _semantic_chunk("0", SEMANTIC_CONTENT)
    keyword = _keyword_chunk("720", KEYWORD_CONTENT)

    fused = retrieval.rrf_fuse([("planned", [semantic]), ("keyword", [keyword])])
    final = rerank.select_final_chunks(fused, [keyword])

    # 融合给出两条：关键字候选本来就在结果里，select 不会再把它插到首位（避免重复）。
    assert len(fused) == 2
    assert [chunk["content"] for chunk in final] == [SEMANTIC_CONTENT, KEYWORD_CONTENT]


def test_multi_route_vector_hits_still_merge_onto_one_slice():
    """验收 3：多路向量召回命中的是同一条入库切片，RRF 合并结果必须不变。"""
    chunk = _semantic_chunk("2", SEMANTIC_CONTENT)

    fused = retrieval.rrf_fuse(
        [("planned", [chunk]), ("hyde", [dict(chunk)]), ("rewrite_1", [dict(chunk)])]
    )

    # rank 在每条路由内各自从 1 起算，三条路由各贡献 1/(60+1)。
    assert len(fused) == 1
    assert fused[0]["rrf_score"] == pytest.approx(3 / 61)
    assert [entry["route"] for entry in fused[0]["routes"]] == ["planned", "hyde", "rewrite_1"]


def test_same_slice_returned_by_keyword_route_is_still_deduped():
    """同一条切片被关键字路再次召回时，仍按「同一段上下文」去重，不重复占配额。"""
    semantic = _semantic_chunk("2", "迟到超过30分钟视为旷工半天")
    duplicate = {**semantic, "route": "keyword", "keyword_score": 12.0}

    final = rerank.select_final_chunks([semantic], [duplicate])

    assert final == [semantic]


def test_identical_text_from_another_file_is_not_deduped():
    """同文判重按文件收敛：另一个文件里的相同文本是另一条上下文。"""
    left = _semantic_chunk("0", SEMANTIC_CONTENT)
    right = _keyword_chunk("0", SEMANTIC_CONTENT, file_id=FILE_ID + 1)

    final = rerank.select_final_chunks([left], [right])

    assert [chunk["file_id"] for chunk in final] == [FILE_ID + 1, FILE_ID]


def test_dedupe_runs_before_truncation_so_a_real_candidate_is_not_pushed_out():
    """同文候选不能先占住 TOP_N 名额再被去掉，把排在后面的真实候选挤掉。"""
    semantic = _semantic_chunk("0", SEMANTIC_CONTENT)
    duplicate = _keyword_chunk("0", SEMANTIC_CONTENT)
    tail = [
        _semantic_chunk(str(index), f"第{index}条 其他内容")
        for index in range(1, rerank.RETRIEVAL_RERANK_TOP_N)
    ]

    final = rerank.select_final_chunks([semantic, duplicate, *tail], [])

    assert len(final) == rerank.RETRIEVAL_RERANK_TOP_N
    assert [chunk["content"] for chunk in final] == [
        SEMANTIC_CONTENT,
        *[chunk["content"] for chunk in tail],
    ]
