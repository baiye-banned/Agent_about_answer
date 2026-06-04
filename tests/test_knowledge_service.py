import pytest
from fastapi import HTTPException

from service import knowledge_service


class _FileEntry:
    def __init__(self, file_id):
        self.id = file_id


def test_delete_knowledge_cleans_vectors_before_mysql_delete(monkeypatch):
    calls = []

    monkeypatch.setattr(knowledge_service.crud_knowledge_file, "get_knowledge_file", lambda db, fid: object())
    monkeypatch.setattr(knowledge_service, "delete_file_chunks", lambda fid: calls.append(("vectors", fid)))
    monkeypatch.setattr(
        knowledge_service.crud_knowledge_file,
        "delete_knowledge_file",
        lambda db, fid: calls.append(("mysql", fid)),
    )

    assert knowledge_service.delete_knowledge(5, db=object()) == {"message": "ok"}
    assert calls == [("vectors", 5), ("mysql", 5)]


def test_delete_knowledge_keeps_mysql_when_vector_cleanup_fails(monkeypatch):
    calls = []

    def fail_vector_cleanup(fid):
        calls.append(("vectors", fid))
        raise RuntimeError("milvus unavailable")

    monkeypatch.setattr(knowledge_service.crud_knowledge_file, "get_knowledge_file", lambda db, fid: object())
    monkeypatch.setattr(knowledge_service, "delete_file_chunks", fail_vector_cleanup)
    monkeypatch.setattr(
        knowledge_service.crud_knowledge_file,
        "delete_knowledge_file",
        lambda db, fid: calls.append(("mysql", fid)),
    )

    with pytest.raises(HTTPException) as exc_info:
        knowledge_service.delete_knowledge(5, db=object())

    assert exc_info.value.status_code == 500
    assert calls == [("vectors", 5)]


def test_delete_knowledge_base_cleans_vectors_before_mysql_delete(monkeypatch):
    calls = []

    monkeypatch.setattr(knowledge_service.crud_knowledge_base, "get_knowledge_base", lambda db, kid: object())
    monkeypatch.setattr(knowledge_service.crud_knowledge_base, "count_knowledge_bases", lambda db: 2)
    monkeypatch.setattr(
        knowledge_service.crud_knowledge_base,
        "get_fallback_knowledge_base",
        lambda db, deleted_id: type("Base", (), {"id": 99})(),
    )
    monkeypatch.setattr(
        knowledge_service.crud_knowledge_base,
        "list_files_for_knowledge_base",
        lambda db, kid: [_FileEntry(7), _FileEntry(8)],
    )
    monkeypatch.setattr(knowledge_service, "delete_file_chunks", lambda file_id: calls.append(("vectors", file_id)))
    monkeypatch.setattr(
        knowledge_service.crud_knowledge_base,
        "delete_knowledge_base_with_files",
        lambda db, kid, fallback_id: calls.append(("mysql", kid, fallback_id)),
    )

    result = knowledge_service.delete_knowledge_base(3, db=object())

    assert result == {"message": "ok", "fallback_knowledge_base_id": 99}
    assert calls == [("vectors", 7), ("vectors", 8), ("mysql", 3, 99)]


def test_delete_knowledge_base_keeps_mysql_when_vector_cleanup_fails(monkeypatch):
    calls = []

    def fail_vector_cleanup(file_id):
        calls.append(("vectors", file_id))
        raise RuntimeError("milvus unavailable")

    monkeypatch.setattr(knowledge_service.crud_knowledge_base, "get_knowledge_base", lambda db, kid: object())
    monkeypatch.setattr(knowledge_service.crud_knowledge_base, "count_knowledge_bases", lambda db: 2)
    monkeypatch.setattr(
        knowledge_service.crud_knowledge_base,
        "get_fallback_knowledge_base",
        lambda db, deleted_id: type("Base", (), {"id": 99})(),
    )
    monkeypatch.setattr(
        knowledge_service.crud_knowledge_base,
        "list_files_for_knowledge_base",
        lambda db, kid: [_FileEntry(7)],
    )
    monkeypatch.setattr(knowledge_service, "delete_file_chunks", fail_vector_cleanup)
    monkeypatch.setattr(
        knowledge_service.crud_knowledge_base,
        "delete_knowledge_base_with_files",
        lambda db, kid, fallback_id: calls.append(("mysql", kid, fallback_id)),
    )

    with pytest.raises(HTTPException) as exc_info:
        knowledge_service.delete_knowledge_base(3, db=object())

    assert exc_info.value.status_code == 500
    assert calls == [("vectors", 7)]
