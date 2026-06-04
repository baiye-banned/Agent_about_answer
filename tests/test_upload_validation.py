from service.utils_service import (
    AVATAR_MAX_BYTES,
    CHAT_ATTACHMENT_MAX_BYTES,
    resolve_image_upload_type,
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
