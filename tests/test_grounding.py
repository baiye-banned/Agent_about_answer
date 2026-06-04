from service.utils_service import _build_sources, _check_answer_grounding, _clip_text, _normalize_grounding_text


def test_clip_text_preserves_falsy_values():
    assert _clip_text(0, 10) == "0"
    assert _clip_text(False, 10) == "False"
    assert _clip_text(None, 10) == ""


def test_build_sources_preserves_falsy_chunk_content():
    sources = _build_sources([
        {"content": 0, "file_id": 1, "file_name": "zero.txt"},
        {"content": False, "file_id": 2, "file_name": "false.txt"},
        {"content": None, "file_id": 3, "file_name": "empty.txt"},
    ])

    assert [source["content"] for source in sources] == ["0", "False"]
    assert [source["excerpt"] for source in sources] == ["0", "False"]


def test_normalize_grounding_text_preserves_falsy_values():
    assert _normalize_grounding_text(0) == "0"
    assert _normalize_grounding_text(False) == "false"
    assert _normalize_grounding_text(None) == ""


def test_grounding_passes_when_answer_matches_context():
    result = _check_answer_grounding(
        "迟到30分钟以上视为旷工。",
        ["公司员工上、下班30分钟以上则视为旷工。"],
    )

    assert result["status"] == "passed"
    assert result["unsupported_count"] == 0


def test_grounding_flags_weakly_supported_claims():
    result = _check_answer_grounding(
        "迟到30分钟以上罚款999元。",
        ["公司员工上、下班30分钟以上则视为旷工。"],
    )

    assert result["status"] == "needs_review"
    assert result["unsupported_count"] == 1
