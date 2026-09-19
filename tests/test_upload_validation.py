import pytest
from fastapi import HTTPException

import config
from service.utils_service import (
    AVATAR_MAX_BYTES,
    CHAT_ATTACHMENT_MAX_BYTES,
    KNOWLEDGE_UPLOAD_MAX_BYTES,
    KNOWLEDGE_UPLOAD_MAX_MB,
    resolve_image_upload_type,
    resolve_knowledge_upload_type,
)


def test_resolve_image_upload_type_accepts_supported_content_type():
    assert resolve_image_upload_type("image/png") == ("image/png", ".png")
    assert resolve_image_upload_type("image/jpeg") == ("image/jpeg", ".jpg")
    assert resolve_image_upload_type("image/webp") == ("image/webp", ".webp")


def test_resolve_image_upload_type_normalizes_content_type():
    assert resolve_image_upload_type(" Image/JPEG ; charset=binary ") == ("image/jpeg", ".jpg")


def test_resolve_image_upload_type_rejects_unknown_content_type():
    assert resolve_image_upload_type("image/gif") is None
    assert resolve_image_upload_type("text/plain") is None


def test_resolve_image_upload_type_can_fallback_to_filename_when_enabled():
    assert resolve_image_upload_type("", "photo.jpg", allow_filename_fallback=True) == ("image/jpeg", ".jpg")


def test_resolve_image_upload_type_does_not_fallback_to_filename_by_default():
    assert resolve_image_upload_type("", "photo.jpg") is None


def test_upload_size_limits_are_named_in_bytes():
    assert CHAT_ATTACHMENT_MAX_BYTES == 5 * 1024 * 1024
    assert AVATAR_MAX_BYTES == 2 * 1024 * 1024
    assert KNOWLEDGE_UPLOAD_MAX_BYTES == KNOWLEDGE_UPLOAD_MAX_MB * 1024 * 1024
    assert KNOWLEDGE_UPLOAD_MAX_MB == config.KNOWLEDGE_UPLOAD_MAX_MB


def test_resolve_knowledge_upload_type_accepts_whitelisted_extensions():
    assert resolve_knowledge_upload_type("text/plain", "notes.txt") == ".txt"
    assert resolve_knowledge_upload_type("text/markdown", "notes.md") == ".md"
    assert resolve_knowledge_upload_type("text/plain", "NOTES.MD") == ".md"
    assert resolve_knowledge_upload_type("application/pdf", "a.pdf") == ".pdf"
    assert (
        resolve_knowledge_upload_type(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "a.docx",
        )
        == ".docx"
    )


def test_resolve_knowledge_upload_type_allows_generic_content_types():
    assert resolve_knowledge_upload_type("application/octet-stream", "a.pdf") == ".pdf"
    assert resolve_knowledge_upload_type(" Application/Octet-Stream ; charset=binary ", "a.txt") == ".txt"
    assert resolve_knowledge_upload_type("", "a.txt") == ".txt"
    assert resolve_knowledge_upload_type(None, "a.txt") == ".txt"


def test_resolve_knowledge_upload_type_rejects_unknown_extensions():
    assert resolve_knowledge_upload_type("text/plain", "run.exe") is None
    assert resolve_knowledge_upload_type("application/octet-stream", "archive.zip") is None
    assert resolve_knowledge_upload_type("text/plain", "README") is None
    assert resolve_knowledge_upload_type("text/plain", None) is None


def test_resolve_knowledge_upload_type_rejects_mismatched_content_type():
    assert resolve_knowledge_upload_type("application/x-msdownload", "notes.txt") is None
    assert resolve_knowledge_upload_type("text/plain", "report.pdf") is None
    assert resolve_knowledge_upload_type("image/png", "a.docx") is None


def test_extract_file_text_rejects_unsupported_extension():
    from crud.knowledge_file import extract_file_text

    assert extract_file_text("a.txt", "内容".encode()) == "内容"
    assert extract_file_text("a.md", "内容".encode()) == "内容"

    with pytest.raises(HTTPException) as excinfo:
        extract_file_text("run.exe", b"MZ")

    assert excinfo.value.status_code == 400
