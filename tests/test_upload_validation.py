import mimetypes

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


# --- issue #237：回退触发条件是「类型不携带格式信息」，不是「类型为空」 ---
#
# 通用类型表（空、application/octet-stream、binary/octet-stream）与知识库侧共用：命中就按
# 扩展名回退；**明确**的类型不回退——放行 text/plain 等于让判据退化成「只看扩展名」。


def test_octet_stream_falls_back_to_the_filename():
    assert resolve_image_upload_type("application/octet-stream", "shot.png", allow_filename_fallback=True) == ("image/png", ".png")


def test_binary_octet_stream_falls_back_to_the_filename():
    assert resolve_image_upload_type("binary/octet-stream", "shot.png", allow_filename_fallback=True) == ("image/png", ".png")


def test_octet_stream_with_parameters_and_case_falls_back_to_the_filename():
    # 归一化先砍参数再去空白/小写，所以带 charset 的变体与裸类型同一类。
    assert resolve_image_upload_type(" Application/Octet-Stream ; charset=binary ", "shot.png", allow_filename_fallback=True) == ("image/png", ".png")


def test_octet_stream_falls_back_to_the_filename_extension():
    assert resolve_image_upload_type("application/octet-stream", "photo.jpg", allow_filename_fallback=True) == ("image/jpeg", ".jpg")


def test_webp_fallback_follows_the_platform_mime_table(monkeypatch):
    """`.webp` 能否按扩展名回退由运行环境的 mime 表决定，不是本模块的判据。

    解释器版本与系统 mime.types 都会影响这张表：本机 Python 3.10 的表里没有 `.webp`，
    回退拿不到类型；CI 的 Linux 表里有，回退就得到 `("image/webp", ".webp")`。两种现实
    都在修复前后一致，不是判据改动引入的开口，所以这里把两种都钉死——两条断言都只走
    「回退表 ∩ 白名单」。
    """
    # 表里没有 `.webp`：回退无类型可给（本机 Python 3.10 的常态）。
    monkeypatch.setattr(mimetypes, "guess_type", lambda name: (None, None))
    assert resolve_image_upload_type("application/octet-stream", "shot.webp", allow_filename_fallback=True) is None
    assert resolve_image_upload_type("", "shot.webp", allow_filename_fallback=True) is None

    # 表里有 `.webp`：回退按扩展名给出类型（CI 的 Linux 表的常态）。
    monkeypatch.setattr(mimetypes, "guess_type", lambda name: ("image/webp", None))
    assert resolve_image_upload_type("application/octet-stream", "shot.webp", allow_filename_fallback=True) == ("image/webp", ".webp")
    assert resolve_image_upload_type("", "shot.webp", allow_filename_fallback=True) == ("image/webp", ".webp")


def test_octet_stream_does_not_fall_back_when_the_caller_did_not_ask_for_it():
    # 回退是调用方显式开的（头像路径就不开）。
    assert resolve_image_upload_type("application/octet-stream", "shot.png") is None


def test_octet_stream_with_an_unsupported_extension_is_still_rejected():
    assert resolve_image_upload_type("application/octet-stream", "notes.txt", allow_filename_fallback=True) is None
    # gif 是图片但不在白名单里：扩展名回退之后仍要落回 IMAGE_UPLOAD_TYPES。
    assert resolve_image_upload_type("application/octet-stream", "shot.gif", allow_filename_fallback=True) is None


def test_octet_stream_without_a_usable_filename_is_rejected():
    assert resolve_image_upload_type("application/octet-stream", None, allow_filename_fallback=True) is None
    assert resolve_image_upload_type("application/octet-stream", "", allow_filename_fallback=True) is None


def test_explicit_non_image_content_type_does_not_fall_back():
    # text/plain 是「明确说了格式」，与「通用/未知」不同类：即便扩展名像图片也不放行。
    assert resolve_image_upload_type("text/plain", "shot.png", allow_filename_fallback=True) is None


def test_empty_content_type_fallback_is_unchanged():
    # 空类型本来就是通用表的成员，修复前就走这条回退：本次修复严格包含旧行为，不是新开口子。
    assert resolve_image_upload_type("", "photo.jpg", allow_filename_fallback=True) == ("image/jpeg", ".jpg")


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
