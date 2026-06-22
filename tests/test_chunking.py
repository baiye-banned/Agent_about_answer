from crud.knowledge_file import chunk_text


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
