
import mimetypes
import re

IMAGE_UPLOAD_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
CHAT_ATTACHMENT_MAX_BYTES = 5 * 1024 * 1024
AVATAR_MAX_BYTES = 2 * 1024 * 1024


def _build_sources(chunks: list[dict]) -> list[dict]:
    sources = []
    for index, chunk in enumerate(chunks, start=1):
        content = _text_value(chunk.get("content")).strip()
        if not content:
            continue
        sources.append({
            "index": index,
            "file_id": chunk.get("file_id", 0),
            "file_name": chunk.get("file_name", "Untitled"),
            "chunk_id": chunk.get("chunk_id", ""),
            "route": chunk.get("route", ""),
            "routes": chunk.get("routes", []),
            "rrf_score": chunk.get("rrf_score"),
            "rerank_score": chunk.get("rerank_score"),
            "rerank_reason": chunk.get("rerank_reason", ""),
            "content": content,
            "excerpt": content[:180] + ("..." if len(content) > 180 else ""),
        })
    return sources


def resolve_image_upload_type(content_type: str | None, filename: str | None = None, *, allow_filename_fallback: bool = False) -> tuple[str, str] | None:
    resolved_type = _normalize_content_type(content_type)
    if not resolved_type and allow_filename_fallback:
        resolved_type = _normalize_content_type(mimetypes.guess_type(filename or "")[0])
    ext = IMAGE_UPLOAD_TYPES.get(resolved_type)
    if not ext:
        return None
    return resolved_type, ext


def _normalize_content_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _clip_text(text: str, max_chars: int) -> str:
    value = "" if text is None else str(text).strip()
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + "..."


def _check_answer_grounding(answer: str, contexts: list[str]) -> dict:
    clean_context = _normalize_grounding_text("\n".join(contexts or []))
    if not answer or not clean_context:
        return {
            "status": "skipped",
            "reason": "missing answer or retrieved contexts",
            "checked_sentences": 0,
            "unsupported_claims": [],
        }

    unsupported = []
    checked = 0
    for sentence in _split_answer_sentences(answer):
        if len(_normalize_grounding_text(sentence)) < 8:
            continue
        checked += 1
        if not _sentence_supported(sentence, clean_context):
            unsupported.append(sentence[:160])
        if len(unsupported) >= 5:
            break

    if checked == 0:
        status = "skipped"
        reason = "no checkable answer sentences"
    elif unsupported:
        status = "needs_review"
        reason = "some answer sentences have weak lexical support in retrieved contexts"
    else:
        status = "passed"
        reason = "answer sentences are lexically supported by retrieved contexts"

    return {
        "status": status,
        "reason": reason,
        "checked_sentences": checked,
        "unsupported_count": len(unsupported),
        "unsupported_claims": unsupported,
    }


def _split_answer_sentences(answer: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"(?<=[。！？!?；;])\s*|\n+", str(answer or ""))
        if item.strip()
    ]


def _sentence_supported(sentence: str, context: str) -> bool:
    normalized = _normalize_grounding_text(sentence)
    if not normalized:
        return True
    numbers = re.findall(r"\d+(?:\.\d+)?", sentence)
    if numbers and not all(number in context for number in numbers):
        return False
    if normalized in context:
        return True
    grams = {normalized[index : index + 2] for index in range(max(len(normalized) - 1, 0))}
    if not grams:
        return True
    shared = sum(1 for gram in grams if gram in context)
    return shared >= 6 and shared / max(len(grams), 1) >= 0.18


def _normalize_grounding_text(text: str) -> str:
    return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9.]+", _text_value(text).lower()))


def _text_value(value) -> str:
    return "" if value is None else str(value)
