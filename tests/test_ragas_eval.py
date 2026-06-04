from rag import ragas_eval


def test_truncate_text_preserves_falsy_values():
    assert ragas_eval._truncate_text(0, 10) == "0"
    assert ragas_eval._truncate_text(False, 10) == "False"
    assert ragas_eval._truncate_text(None, 10) == ""


def test_prepare_contexts_preserves_falsy_values(monkeypatch):
    monkeypatch.setattr(ragas_eval, "RAGAS_MAX_CONTEXTS", 4)
    monkeypatch.setattr(ragas_eval, "RAGAS_MAX_CONTEXT_CHARS", 20)

    assert ragas_eval._prepare_contexts([0, False, None, "", "制度"]) == ["0", "False", "制度"]
