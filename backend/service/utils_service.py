
import json


def _build_sources(chunks: list[dict]) -> list[dict]:
    sources = []
    for index, chunk in enumerate(chunks, start=1):
        content = (chunk.get("content") or "").strip()
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


def _loads_json(value: str, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _clip_text(text: str, max_chars: int) -> str:
    value = (text or "").strip()
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + "..."
