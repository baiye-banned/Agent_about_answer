"""issue #59：关键词召回的成本必须与「命中的内容量」相关，而不是与知识库总量相关。

旧实现每次提问都把整库文件的 LONGTEXT ``content`` 读进内存、逐文件整篇归一化、再逐窗口
重新归一化，最后只返回 ``top_k`` 条。这个文件锁住两件事：

* **取数上界**：召回路径发出的 SQL 必须带 ``LIMIT``（按主键游标分批），并且不能把「没有
  命中的文件」拉进内存——用一个远大于 ``top_k`` 的知识库断言扫描量/加载量有上界；
* **结果不回退**：把修复前的实现原样冻结在本文件里（``_legacy_keyword_recall``），对同一
  批语料逐条比对返回内容、顺序、分值与命中证据。

冻结副本刻意不复用 ``backend/rag/retrieval.py`` 里的打分/切分函数：如果两边共用同一份实现，
「两边一起改错」就测不出来了。只有与本次修复无关的 ``_expand_keywords``（查询关键词展开）
沿用模块实现。
"""

from __future__ import annotations

import math
import random
import tracemalloc

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.session import Base
from model.models import KnowledgeFile
from rag import retrieval


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_text(_type, _compiler, **_kwargs):
    """The model declares MySQL LONGTEXT; SQLite needs it spelled as TEXT."""
    return "TEXT"


KB = 7
OTHER_KB = 8


# ---------------------------------------------------------------------------
# 冻结的旧实现（issue #59 修复前，逐行等价于 develop 上的 rag.retrieval）
# ---------------------------------------------------------------------------


def _legacy_normalize(text) -> str:
    import re

    value = "" if text is None else str(text)
    return re.sub(r"\s+", "", value.lower())


def _legacy_split_chunks(content, chunk_size=900, chunk_overlap=180):
    chunks = []
    step = max(chunk_size - chunk_overlap, 1)
    for start in range(0, len(content), step):
        text = content[start : start + chunk_size].strip()
        if text:
            chunks.append({"chunk_id": str(start), "content": text})
    return chunks


def _legacy_has_close_matches(content, keywords, window=120):
    positions = []
    for keyword in keywords:
        normalized_keyword = _legacy_normalize(keyword)
        if len(normalized_keyword) < 2:
            continue
        pos = content.find(normalized_keyword)
        if pos >= 0:
            positions.append(pos)
    if len(positions) < 3:
        return False
    positions.sort()
    return any(positions[index + 2] - positions[index] <= window for index in range(len(positions) - 2))


def _legacy_keyword_score(content, keywords):
    normalized_content = _legacy_normalize(content)
    score = 0.0
    matched_positions = []
    for keyword in keywords:
        normalized_keyword = _legacy_normalize(keyword)
        if len(normalized_keyword) < 2:
            continue
        count = normalized_content.count(normalized_keyword)
        if count <= 0:
            continue
        weight = 1.0
        if any(char.isdigit() for char in normalized_keyword):
            weight += 3.0
        if len(normalized_keyword) >= 4:
            weight += 2.0
        if normalized_keyword in {"迟到", "早退", "旷工", "罚款", "处罚", "考勤"}:
            weight += 4.0
        score += count * weight
        matched_positions.append(normalized_content.find(normalized_keyword))
    unique_hits = len({pos for pos in matched_positions if pos >= 0})
    score += unique_hits * 1.5
    if _legacy_has_close_matches(normalized_content, keywords):
        score += 8.0
    return score


def _legacy_matched_query_keywords(content, keywords):
    normalized_content = _legacy_normalize(content)
    matched = []
    seen = set()
    for keyword in keywords:
        normalized_keyword = _legacy_normalize(keyword)
        if len(normalized_keyword) < 2 or normalized_keyword in seen:
            continue
        if normalized_keyword in normalized_content:
            seen.add(normalized_keyword)
            matched.append(keyword)
    return matched


def _legacy_keyword_recall(db, knowledge_base_id, keywords, top_k):
    """修复前的 keyword_recall（冻结副本）。

    唯一的有意差异：旧实现没有 ``ORDER BY``，行序由数据库决定（SQLite/InnoDB 实际按主键
    返回）。这里显式按主键取，让「同分候选的先后」有确定的比较基准。
    """
    clean_keywords = retrieval._expand_keywords(keywords)
    if not clean_keywords:
        return []
    candidates = []
    files = (
        db.query(KnowledgeFile)
        .filter_by(knowledge_base_id=knowledge_base_id)
        .order_by(KnowledgeFile.id)
        .all()
    )
    for file_entry in files:
        content = file_entry.content or ""
        normalized_content = _legacy_normalize(content)
        if not any(_legacy_normalize(keyword) in normalized_content for keyword in clean_keywords):
            continue
        for chunk in _legacy_split_chunks(content):
            matched_keywords = _legacy_matched_query_keywords(chunk["content"], clean_keywords)
            if not matched_keywords:
                continue
            score = _legacy_keyword_score(chunk["content"], clean_keywords)
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
                    "keyword_hits": len(matched_keywords),
                    "matched_keywords": matched_keywords,
                }
            )
    candidates.sort(key=lambda item: item["keyword_score"], reverse=True)
    return candidates[:top_k]


# ---------------------------------------------------------------------------
# 测试用真实数据库
# ---------------------------------------------------------------------------


def _new_db(tmp_path, name="keyword-recall-59.db"):
    engine = create_engine(
        f"sqlite:///{(tmp_path / name).as_posix()}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=[KnowledgeFile.__table__])
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()
    return engine, db


def _add_files(db, rows, knowledge_base_id=KB):
    db.add_all(
        [
            KnowledgeFile(knowledge_base_id=knowledge_base_id, name=name, size=len(content), content=content)
            for name, content in rows
        ]
    )
    db.commit()
    db.expunge_all()


def _fingerprint(chunks):
    return [
        (
            chunk["id"],
            chunk["chunk_id"],
            chunk["file_name"],
            chunk["file_id"],
            chunk["route"],
            chunk["keyword_score"],
            chunk["keyword_hits"],
            chunk["matched_keywords"],
            chunk["content"],
        )
        for chunk in chunks
    ]


def _record_selects(engine):
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        if not executemany and statement.strip().upper().startswith("SELECT"):
            statements.append(" ".join(statement.split()))

    return statements


def _assert_matches_legacy(db, keywords, top_k):
    expected = _legacy_keyword_recall(db, KB, keywords, top_k)
    actual = retrieval.keyword_recall(db, KB, keywords, top_k)
    assert _fingerprint(actual) == _fingerprint(expected)
    return actual


# ---------------------------------------------------------------------------
# 等价性：修复前后返回的候选逐条一致
# ---------------------------------------------------------------------------

ATTENDANCE = "员工迟到30分钟以内罚款50元；迟到超过30分钟按旷工处理。"
ASSESSMENT = "考勤制度：上下班迟到早退均计入月度考核。"
EXPENSE = "报销流程：发票需要在30天内提交。"
LONG_STEP = 900 - 180
LONG_FILE = "迟到超过30分钟按旷工处理。" + "附" * (LONG_STEP - 12) + "迟到罚款50元。"


def _mixed_corpus():
    rng = random.Random(20260921)
    words = ["迟到", "旷工", "考勤", "报销", "发票", "制度", "员工", "处罚", "30分钟", "旷工处理"]
    spacers = ["", " ", "\n", "\t", "　", " ", "  \n  "]
    documents = []
    for index in range(12):
        pieces = []
        for _ in range(90):
            pieces.append(rng.choice(words))
            pieces.append(rng.choice(spacers))
        documents.append((f"混合{index}.txt", "".join(pieces)))
    return documents


def test_keyword_recall_matches_legacy_on_mixed_corpus(tmp_path):
    _, db = _new_db(tmp_path)
    _add_files(
        db,
        [
            ("考勤制度.txt", ATTENDANCE),
            ("考核.txt", ASSESSMENT),
            ("报销.txt", EXPENSE),
            ("长文件.txt", LONG_FILE),
            ("空文件.txt", ""),
            ("全空白.txt", " \n\t　  \n"),
            ("跨窗口.txt", "迟" + "填" * (LONG_STEP - 1) + "到" + "罚款50元。" * 5),
            ("同一段落.txt", ATTENDANCE),
            *_mixed_corpus(),
        ],
    )
    for keywords, top_k in [
        (["迟到"], 8),
        (["迟到", "旷工"], 8),
        (["报销", "发票"], 3),
        (["迟到", "旷工", "考勤", "处罚"], 1),
        (["迟到", "旷工"], 1000),
        (["不存在的关键词"], 8),
        (["30分钟"], 5),
        (["迟到 旷工"], 8),
        (["   "], 8),
        ([], 8),
    ]:
        _assert_matches_legacy(db, keywords, top_k)


def test_keyword_recall_matches_legacy_for_case_and_unicode_keywords(tmp_path):
    _, db = _new_db(tmp_path)
    _add_files(
        db,
        [
            ("英文.txt", "Annual Leave Policy: employees apply LEAVE in advance. DRAFT only."),
            ("中文.txt", ATTENDANCE),
            ("俄文.txt", "ПРИВЕТ и привет: правила внутреннего распорядка."),
        ],
    )
    for keywords in [
        ["leave"],
        ["LEAVE"],
        ["LeAvE"],
        ["LOOSE"],
        ["привет"],
        ["ПРИВЕТ"],
        ["привет", "迟到"],
    ]:
        _assert_matches_legacy(db, keywords, 8)


def test_keyword_recall_matches_legacy_for_overlapping_and_repeated_keywords(tmp_path):
    _, db = _new_db(tmp_path)
    _add_files(
        db,
        [
            ("重复.txt", "哈哈哈哈" * 40 + "哈" * 5),
            ("周期.txt", "abab" * 60),
            ("无空白重复.txt", "劳" * 2000),
            ("数字.txt", "1111" * 50 + " 1111 1111"),
        ],
    )
    for keywords in [["哈哈"], ["ab"], ["abab"], ["劳"], ["11"], ["1111"], ["111"]]:
        _assert_matches_legacy(db, keywords, 8)


def test_keyword_recall_matches_legacy_across_window_boundaries(tmp_path):
    _, db = _new_db(tmp_path)
    filler = "本制度由行政部负责解释。"
    documents = []
    for offset in range(0, 1000, 37):
        content = ("迟" * 0) + filler * (offset // len(filler) + 1)
        content = content + "迟到" + filler * 40
        documents.append((f"边界{offset}.txt", content[offset:]))
    _add_files(db, documents)
    _assert_matches_legacy(db, ["迟到", "行政部"], 8)


def test_keyword_recall_matches_keyword_split_by_whitespace(tmp_path):
    """关键词的字符在正文里被空白隔开时也必须命中（归一化会去掉空白）。

    这是 SQL 预筛最容易写错的地方：正文写成「考 假」时去掉空白才等于「考假」，如果预筛
    按字面子串下推（``content LIKE '%考假%'``），整个文件会在取数层就被丢掉，召回静默
    少一条。这里的用例就是为这条路径准备的。
    """
    _, db = _new_db(tmp_path)
    _add_files(
        db,
        [
            ("跨空格.txt", "本制度由行政部负责解释。考 假 请 流 程。"),
            ("跨换行.txt", "本制度由行政部负责解释。考\n假\n请 流程。"),
            ("跨全角空格.txt", "本制度由行政部负责解释。考　假　请 流程。"),
            ("无关.txt", "报销流程：发票需要在30天内提交。"),
        ],
    )

    chunks = _assert_matches_legacy(db, ["考假请"], 8)

    assert chunks
    assert {chunk["file_name"] for chunk in chunks} == {"跨空格.txt", "跨换行.txt", "跨全角空格.txt"}


def test_keyword_recall_handles_like_metacharacters_in_keywords(tmp_path):
    """关键词里的 ``%``/``_``/``!``/``\\`` 不能改变预筛语义（转义后仍是必要条件）。"""
    _, db = _new_db(tmp_path)
    _add_files(
        db,
        [
            ("百分号.txt", "报销比例 100% 以内的部分由公司承担。"),
            ("下划线.txt", "字段 kb_id 与 kbXid 都要登记。"),
            ("反斜杠.txt", "路径 C:\\制度\\考勤 下的文件需要归档。"),
            ("感叹号.txt", "注意！迟到要扣钱。"),
            ("无关.txt", "发票需要在30天内提交。"),
        ],
    )
    for keywords in [["100%"], ["kb_id"], ["kbXid"], ["C:\\制度"], ["！迟到"], ["%"], ["_"], ["\\"]]:
        _assert_matches_legacy(db, keywords, 8)


def test_keyword_recall_does_not_leak_other_knowledge_bases(tmp_path):
    _, db = _new_db(tmp_path)
    _add_files(db, [("本库.txt", ATTENDANCE)], knowledge_base_id=KB)
    _add_files(db, [("他库.txt", ATTENDANCE * 3)], knowledge_base_id=OTHER_KB)

    chunks = _assert_matches_legacy(db, ["迟到", "旷工"], 8)

    assert [chunk["file_name"] for chunk in chunks] == ["本库.txt"]
    assert retrieval.keyword_recall(db, KB + 999, ["迟到"], 8) == []


# ---------------------------------------------------------------------------
# 取数上界：SQL 带 LIMIT / 游标分批，且不加载未命中的文件
# ---------------------------------------------------------------------------


def test_keyword_recall_sql_is_paged_and_prefiltered(tmp_path):
    engine, db = _new_db(tmp_path)
    _add_files(
        db,
        [(f"命中{index}.txt", ATTENDANCE) for index in range(9)]
        + [(f"无关{index}.txt", EXPENSE) for index in range(40)],
    )
    statements = _record_selects(engine)

    chunks = retrieval.keyword_recall(db, KB, ["迟到", "旷工"], 8)

    assert len(chunks) == 8
    assert statements, "keyword_recall 应当只发 SELECT"
    # 主查询必须带 LIMIT（分批），并且只扫「可能命中」的行（LIKE 预筛已下推到 SQL）。
    assert all(" LIMIT " in statement.upper() for statement in statements)
    assert any(" LIKE " in statement.upper() for statement in statements)
    # 命中的只有 9 个文件，批大小 4：往返次数必须是常数级，而不是每个文件一次。
    assert len(statements) <= math.ceil(9 / retrieval.KEYWORD_RECALL_BATCH_SIZE) + 1


def test_keyword_recall_does_not_load_unmatched_file_bodies(tmp_path):
    def run(name, noise_files):
        _, db = _new_db(tmp_path, name=name)
        _add_files(db, [("命中.txt", ATTENDANCE), ("命中2.txt", ATTENDANCE)])
        _add_files(db, [(f"无关{index}.txt", EXPENSE * 8000) for index in range(noise_files)])
        db.expunge_all()
        tracemalloc.start()
        chunks = retrieval.keyword_recall(db, KB, ["迟到"], 8)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert chunks, "命中文档必须仍然被召回"
        return peak

    only_relevant = run("peak-few.db", 0)
    with_noise = run("peak-many.db", 30)

    # 30 个无关文件合计约 380 万字符：旧实现会把它们全部读进内存（实测峰值多出 7MB 以上），
    # 新实现只按主键分批取「过筛」的行，多出的内存可以忽略。
    assert with_noise - only_relevant < 2_000_000


def test_keyword_recall_returns_nothing_for_empty_inputs(tmp_path):
    _, db = _new_db(tmp_path)
    _add_files(db, [("考勤.txt", ATTENDANCE)])

    assert retrieval.keyword_recall(db, KB, [], 8) == []
    assert retrieval.keyword_recall(db, KB, ["   "], 8) == []
    assert retrieval.keyword_recall(db, KB, ["迟到"], 0) == []
    assert retrieval.keyword_recall(db, KB + 999, ["迟到"], 8) == []


def test_keyword_recall_survives_null_content(tmp_path):
    _, db = _new_db(tmp_path)
    db.add(KnowledgeFile(knowledge_base_id=KB, name="空.txt", size=0, content=None))
    db.add(KnowledgeFile(knowledge_base_id=KB, name="有内容.txt", size=len(ATTENDANCE), content=ATTENDANCE))
    db.commit()
    db.expunge_all()

    _assert_matches_legacy(db, ["迟到"], 8)
