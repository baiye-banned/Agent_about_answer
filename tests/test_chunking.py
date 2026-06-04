from crud.knowledge_file import chunk_text


def test_chunk_text_tolerates_missing_text():
    assert chunk_text(None, file_id=1) == []


def test_chunk_text_keeps_section_headings_and_clause_context():
    text = (
        "三、考勤\n"
        "1、公司员工上、下班（30分钟以内为迟到或早退、30分钟以上则视为旷工）迟到、早退一次，罚款50元，"
        "二次，罚款200元，月累计三次及以上情节严重者，降职使用或按自动离职处理。\n"
        "2、无故不办理请假手续，而擅自不上班，按旷工处理。\n"
        "四、事假\n"
        "公司员工因事需请假，须持书面请假报告，经部门经理签字同意报综合管理部。"
    )

    chunks = chunk_text(text, file_id=1, chunk_size=120, chunk_overlap=20)

    assert any(chunk["text"].startswith("三、考勤") for chunk in chunks)
    assert any("月累计三次及以上情节严重者" in chunk["text"] for chunk in chunks)
    assert any(chunk["text"].startswith("四、事假") for chunk in chunks)
    assert not any(chunk["text"].startswith("罚款200元") for chunk in chunks)
