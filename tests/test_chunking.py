import pytest

from crud.knowledge_file import (
    KNOWLEDGE_INDEX_MIN_COVERAGE_RATIO,
    _LONG_HEADING_MAX_LEN,
    _ORDER_HEADING_TAIL_MAX_LEN,
    _heading_level,
    chunk_coverage_ratio,
    chunk_text,
)


# issue #53 的语料：条款编号与正文同一行，正文句在整篇文档里大量重复。
CLAUSE_COUNT = 40
SENTENCES_PER_CLAUSE = 3
CLAUSE_BODY = "本制度适用于全体员工，由人事部负责解释与修订。"
CLAUSE_LINES = [f"第{i}条 {CLAUSE_BODY * SENTENCES_PER_CLAUSE}" for i in range(1, CLAUSE_COUNT + 1)]

# issue #75 的语料：中文/括号/数字编号与正文同一行，行间不空行。
ORDER_ROWS = 40
ORDER_PREFIXES = ["三、", "（一）", "1、", "1. "]
ORDER_BODY = CLAUSE_BODY
LONG_ORDER_BODY_REPEATS = 6
LONG_ORDER_BODY = ORDER_BODY * LONG_ORDER_BODY_REPEATS
# 长度边界语料：44 字正文配上最长的 3 字标记「（一）」后整行恰好 48 字。
# issue #83 之后长度信号量在「标记之后的文本」上，本语料由 44 > _ORDER_HEADING_TAIL_MAX_LEN
# 判为正文，整行长度不再参与判定。
BOUNDARY_SENTENCE = "本制度适用于全体员工，由人事部门解释与修订。"
BOUNDARY_BODY = f"{BOUNDARY_SENTENCE}{ORDER_BODY}"

# issue #83 第 1 项语料：编号与正文同行、正文**不带句末标点**，行间不空行。
# 句末标点信号在这里完全失效，只能靠「标记之后的文本长度」救回正文。
PHRASE_BODY = "员工迟到30分钟以内罚款50元，由人事部汇总"
PHRASE_PREFIXES = ["三、", "（一）", "1、", "1. "]

# issue #83 第 2 项：八种前缀（标记长度 2~4 字）的「标题 ↔ 正文」边界必须落在同一处。
TAIL_BOUNDARY_PREFIXES = ["三、", "十、", "1、", "（一）", "(一)", "十一、", "1. ", "第三条 "]


def _assert_clause_document_fully_indexed(source: str, chunks: list[dict]) -> None:
    """入库文本合计覆盖原文 ≥ 90%，且每条条款编号都真的进了库（无损）。"""
    assert chunk_coverage_ratio(source, chunks) >= 0.9
    joined = "\n".join(chunk["text"] for chunk in chunks)
    for index in range(1, CLAUSE_COUNT + 1):
        assert f"第{index}条" in joined
    assert joined.count(CLAUSE_BODY) >= CLAUSE_COUNT * SENTENCES_PER_CLAUSE


def _assert_order_document_fully_indexed(source: str, chunks: list[dict], body: str, expected_count: int) -> None:
    """入库文本合计覆盖原文 ≥ 90%，且每一行正文的每一句都真的进了库（无损）。"""
    assert chunk_coverage_ratio(source, chunks) >= 0.9
    joined = "\n".join(chunk["text"] for chunk in chunks)
    assert joined.count(body) >= expected_count


def test_chunk_text_tolerates_missing_text():
    assert chunk_text(None, file_id=1) == []


def test_chunk_text_keeps_heading_path_for_body_chunks():
    text = """
第一章 总则
第一节 适用范围
本制度适用于公司所有正式员工。
员工应当遵守考勤、请假和信息安全要求。

第二节 职责分工
人力资源部负责制度解释。
各部门负责人负责日常执行。
"""

    chunks = chunk_text(text, file_id=1)

    assert chunks
    assert any(
        "第一章 总则" in chunk["text"]
        and "第一节 适用范围" in chunk["text"]
        and "本制度适用于公司所有正式员工。" in chunk["text"]
        for chunk in chunks
    )
    assert any(
        "第一章 总则" in chunk["text"]
        and "第二节 职责分工" in chunk["text"]
        and "人力资源部负责制度解释。" in chunk["text"]
        for chunk in chunks
    )


def test_chunk_text_ignores_pdf_page_heading_as_context_heading():
    text = """
第 1 页
第一章 总则
本制度用于说明知识库上传规范。

第 2 页
第一节 文件要求
PDF 文件需要包含可复制文本。
"""

    chunks = chunk_text(text, file_id=1)

    assert len(chunks) == 2
    assert all("第 1 页" not in chunk["text"] and "第 2 页" not in chunk["text"] for chunk in chunks)
    assert chunks[0]["text"].startswith("第一章 总则")
    assert "第一章 总则" in chunks[1]["text"]
    assert "第一节 文件要求" in chunks[1]["text"]


def test_chunk_text_splits_long_section_with_heading_path_and_overlap():
    paragraphs = [
        f"第{i}段内容说明审批流程、职责边界和执行要求，确保上传后的知识库可以稳定检索。"
        for i in range(1, 9)
    ]
    text = "第一章 流程规范\n第一节 上传要求\n" + "\n\n".join(paragraphs)

    chunks = chunk_text(text, file_id=1, chunk_size=150, chunk_overlap=80)

    assert len(chunks) > 1
    assert all("第一章 流程规范" in chunk["text"] for chunk in chunks)
    assert all("第一节 上传要求" in chunk["text"] for chunk in chunks)
    assert "第3段内容说明" in chunks[0]["text"]
    assert "第3段内容说明" in chunks[1]["text"]


def test_chunk_text_treats_long_numbered_clause_as_body():
    long_clause = (
        "1、公司员工上、下班迟到或早退一次，罚款50元，二次罚款100元，"
        "月累计三次及以上属于严重违纪，按照公司制度进一步处理。"
    )
    text = f"三、考勤\n{long_clause}\n2、短标题\n短标题下的正文。"

    chunks = chunk_text(text, file_id=1, chunk_size=220, chunk_overlap=60)

    assert chunks[0]["text"].startswith("三、考勤")
    assert long_clause in chunks[0]["text"]
    assert not any(chunk["text"].startswith(long_clause) for chunk in chunks)
    assert any("三、考勤" in chunk["text"] and "2、短标题" in chunk["text"] for chunk in chunks)


def test_heading_level_treats_clause_line_with_inline_body_as_body():
    """issue #53：42 字的示例行低于 48 字长度阈值，必须靠句末标点判定为正文。"""
    line = "第十条 员工迟到30分钟以内罚款50元，由人事部按月汇总后归档保存，保存期限为三年。"
    assert len(line) < 48
    assert _heading_level(line) is None


def test_heading_level_treats_overlong_clause_line_as_body():
    line = "第十条 " + "罚款标准与执行口径说明" * 5
    assert len(line) > 48
    assert _heading_level(line) is None


def test_heading_level_keeps_clause_heading_without_inline_body():
    """条款标记后没有正文的行仍然是标题，标题层级不被破坏。"""
    assert _heading_level("第一章 总则") == 1
    assert _heading_level("第十条") == 2
    assert _heading_level("第十条 罚款标准") == 2


def test_chunk_coverage_ratio_reports_share_of_source_text():
    assert chunk_coverage_ratio("", []) == 1.0
    assert chunk_coverage_ratio("第一段", []) == 0.0
    assert chunk_coverage_ratio("第一段", [{"text": "第一"}, {"text": "段"}]) == 1.0
    assert chunk_coverage_ratio("第一段落", [{"text": "第一"}]) == 0.5
    assert KNOWLEDGE_INDEX_MIN_COVERAGE_RATIO == 0.5


def test_chunk_text_keeps_clause_text_when_heading_shares_line():
    """条款与正文同行、条款之间无空行的制度文档，正文不能被整行当标题丢掉。"""
    source = "\n".join(CLAUSE_LINES)

    chunks = chunk_text(source, file_id=1)

    _assert_clause_document_fully_indexed(source, chunks)


def test_chunk_text_keeps_clause_text_when_clauses_are_blank_line_separated():
    """同样排版但条款之间空一行，入库覆盖率同样要达标。"""
    source = "\n\n".join(CLAUSE_LINES)

    chunks = chunk_text(source, file_id=1)

    _assert_clause_document_fully_indexed(source, chunks)


def test_chunk_text_keeps_clause_text_when_number_sits_on_its_own_line():
    """对照排版：编号单独占一行，同样要无损，分块数量保持同一量级（约 40）。"""
    source = "\n".join(f"第{i}条\n{CLAUSE_BODY * SENTENCES_PER_CLAUSE}" for i in range(1, CLAUSE_COUNT + 1))

    chunks = chunk_text(source, file_id=1)

    _assert_clause_document_fully_indexed(source, chunks)
    assert len(chunks) >= 20


@pytest.mark.parametrize("prefix", ORDER_PREFIXES)
def test_chunk_text_keeps_order_text_when_marker_shares_short_line(prefix):
    """issue #75：「三、」「（一）」「1、」「1.」与短正文同行（每行 ≤48 字）时不能整行丢弃。"""
    source = "\n".join(f"{prefix}{ORDER_BODY}" for _ in range(ORDER_ROWS))

    chunks = chunk_text(source, file_id=1)

    _assert_order_document_fully_indexed(source, chunks, ORDER_BODY, ORDER_ROWS)


@pytest.mark.parametrize("prefix", ORDER_PREFIXES)
def test_chunk_text_keeps_order_text_when_marker_shares_long_line(prefix):
    """issue #75：同样排版换成超长正文（每行 >48 字），同样要无损入库。

    这几种前缀原本靠 _looks_like_long_list_item 的长度阈值侥幸兜住长行，
    覆盖率高只是巧合；统一走「标记后是否跟正文」判定后不再依赖阈值。
    """
    source = "\n".join(f"{prefix}{LONG_ORDER_BODY}" for _ in range(ORDER_ROWS))

    chunks = chunk_text(source, file_id=1)

    _assert_order_document_fully_indexed(
        source, chunks, ORDER_BODY, ORDER_ROWS * LONG_ORDER_BODY_REPEATS
    )


@pytest.mark.parametrize("prefix", ORDER_PREFIXES)
def test_chunk_text_keeps_order_text_at_heading_length_boundary(prefix):
    """issue #75 边界语料：整行不超过 48 字，四种前缀同样要无损入库。

    issue #83 第 2 项之后长度信号量在「标记之后的文本」上，本语料的 44 字正文
    已超过 _ORDER_HEADING_TAIL_MAX_LEN，由长度信号兜住。整行长度不再参与判定，
    所以这里不再断言整行恰好 48 字——那是旧口径下的边界。
    """
    line = f"{prefix}{BOUNDARY_BODY}"
    assert len(line) <= _LONG_HEADING_MAX_LEN
    assert len(BOUNDARY_BODY) > _ORDER_HEADING_TAIL_MAX_LEN
    source = "\n".join(line for _ in range(ORDER_ROWS))

    chunks = chunk_text(source, file_id=1)

    _assert_order_document_fully_indexed(source, chunks, BOUNDARY_SENTENCE, ORDER_ROWS)


@pytest.mark.parametrize("prefix", PHRASE_PREFIXES)
def test_chunk_text_keeps_order_text_when_inline_body_has_no_sentence_punctuation(prefix):
    """issue #83 第 1 项：标记后跟不带句末标点的短语正文时，整行不能被当标题丢掉。

    先证红：修复前这 40 行全部命中标题分支，chunk_text 只剩兜底的最后一行，
    覆盖率 2.4%（1 个 chunk / 24 字 / 原文 999 字）；补一个句末标点就回到 100%。
    修复后必须 ≥ 90%，且每一行的正文都要真的进库（覆盖率高不能靠重复行凑）。
    """
    line = f"{prefix}{PHRASE_BODY}"
    source = "\n".join(line for _ in range(ORDER_ROWS))

    chunks = chunk_text(source, file_id=1)

    assert chunk_coverage_ratio(source, chunks) >= 0.9
    joined = "\n".join(chunk["text"] for chunk in chunks)
    assert joined.count(PHRASE_BODY) >= ORDER_ROWS


@pytest.mark.parametrize("prefix", PHRASE_PREFIXES)
def test_chunk_text_keeps_every_distinct_phrase_row(prefix):
    """同上语料但逐行互不相同：每一行的正文都要单独进库，不能靠重复行拉高覆盖率。"""
    rows = [f"{prefix}员工迟到{index}分钟以内罚款50元，由人事部汇总" for index in range(1, ORDER_ROWS + 1)]
    source = "\n".join(rows)

    chunks = chunk_text(source, file_id=1)

    assert chunk_coverage_ratio(source, chunks) >= 0.9
    joined = "\n".join(chunk["text"] for chunk in chunks)
    for index in range(1, ORDER_ROWS + 1):
        assert f"员工迟到{index}分钟以内罚款50元" in joined


@pytest.mark.parametrize("prefix", TAIL_BOUNDARY_PREFIXES)
def test_heading_level_uses_one_tail_boundary_for_every_prefix(prefix):
    """issue #83 第 2 项：八种前缀的有效截断点统一在「标记之后的文本」长度上。

    修复前阈值量在整行上，标记本身的 2~4 字被一起计入，边界随前缀漂移
    （正文 46 字时「三、」已判正文而「（一）」仍被丢弃）；现在标记长度不再影响判定，
    同一段正文换任何前缀都在同一个字符数上翻转。
    """
    heading_tail = "甲" * _ORDER_HEADING_TAIL_MAX_LEN
    body_tail = "甲" * (_ORDER_HEADING_TAIL_MAX_LEN + 1)

    assert _heading_level(f"{prefix}{heading_tail}") is not None
    assert _heading_level(f"{prefix}{body_tail}") is None


def test_heading_level_keeps_short_inline_tail_as_heading():
    """边界另一侧：标记后的短文本（≤ 阈值且无句末标点）仍是标题，层级语义不被破坏。"""
    assert _heading_level("三、考勤管理") == 2
    assert _heading_level("（一）适用范围") == 3
    assert _heading_level("第三条 罚款标准") == 2


@pytest.mark.parametrize("prefix", ORDER_PREFIXES)
def test_chunk_text_keeps_order_text_when_marker_sits_on_its_own_line(prefix):
    """对照排版：编号单独占一行，四种前缀的覆盖率同样不能回退。"""
    source = "\n".join(f"{prefix}\n{ORDER_BODY}" for _ in range(ORDER_ROWS))

    chunks = chunk_text(source, file_id=1)

    assert chunk_coverage_ratio(source, chunks) >= 0.9


@pytest.mark.parametrize("prefix", ORDER_PREFIXES)
def test_chunk_text_indexes_every_order_row(prefix):
    """issue #75：逐行核对每一条编号都真的进了库。

    上面的用例沿用 issue 的语料（40 行内容相同），覆盖率是「长度求和」，
    理论上「同一行重复 40 次」也能凑够；这条用例让每行互不相同再逐行断言。
    """
    source = "\n".join(f"{prefix}第{index}项 {ORDER_BODY}" for index in range(1, ORDER_ROWS + 1))

    chunks = chunk_text(source, file_id=1)

    assert chunk_coverage_ratio(source, chunks) >= 0.9
    joined = "\n".join(chunk["text"] for chunk in chunks)
    for index in range(1, ORDER_ROWS + 1):
        assert f"{prefix}第{index}项" in joined


def test_heading_level_treats_overlong_decimal_chain_as_body():
    """issue #75 复核：整行只由数字与点号组成的小数链仍按正文处理。

    _DECIMAL_MARKER_RE 会把这种行整行吃光，统一判定取到的标记后文本为空、
    长度信号失效，只能由 _looks_like_long_list_item 拦下——它不能被合并掉。
    """
    line = "1.2.3.4.5.6.7.8.9.10.11.12.13.14.15.16.17.18.19.20.1."
    assert len(line) > _LONG_HEADING_MAX_LEN
    assert _heading_level(line) is None


def test_heading_level_keeps_order_heading_without_inline_body():
    """issue #75：标记后没有正文的行仍是标题，标题语义不被误判成正文。"""
    assert _heading_level("三、总则") == 2
    assert _heading_level("（一）总则") == 3
    assert _heading_level("1、总则") == 3
    # 「1.」前缀的层级沿用修复前的判定（2），本次只改「是否按正文处理」。
    assert _heading_level("1. 总则") == 2


@pytest.mark.parametrize("prefix", ORDER_PREFIXES)
def test_heading_level_treats_order_line_with_inline_body_as_body(prefix):
    """issue #75：标记后跟成句正文时按正文处理（短行靠句末标点，长行靠长度阈值）。"""
    assert _heading_level(f"{prefix}{ORDER_BODY}") is None
    assert _heading_level(f"{prefix}{LONG_ORDER_BODY}") is None
