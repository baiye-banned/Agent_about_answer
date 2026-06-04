import hashlib
import logging
import os
from typing import Optional

import httpx
import numpy as np

from config import (
    EMBEDDING_API_KEY,
    EMBEDDING_BASE_URL,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    MILVUS_COLLECTION_NAME,
    MILVUS_DB_NAME,
    MILVUS_PASSWORD,
    MILVUS_TOKEN,
    MILVUS_URI,
    MILVUS_USER,
    RETRIEVAL_ROUTE_TOP_K,
)


def _is_lite_uri(uri: str) -> bool:
    return "://" not in (uri or "")


def _hide_lite_uri_from_pymilvus_import() -> str | None:
    """pymilvus treats MILVUS_URI as a server URI during import; Lite uses a file path."""
    if not _is_lite_uri(MILVUS_URI):
        return None
    return os.environ.pop("MILVUS_URI", None)


def _restore_milvus_uri_env(value: str | None) -> None:
    if value is not None:
        os.environ["MILVUS_URI"] = value


_milvus_uri_env = _hide_lite_uri_from_pymilvus_import()

from pymilvus import DataType, MilvusClient
from pymilvus.orm.schema import CollectionSchema, FieldSchema

_restore_milvus_uri_env(_milvus_uri_env)

_client: MilvusClient | None = None
logger = logging.getLogger(__name__)


def _collection_name() -> str:
    if MILVUS_COLLECTION_NAME:
        return MILVUS_COLLECTION_NAME
    model_part = "".join(ch if ch.isalnum() else "_" for ch in EMBEDDING_MODEL.lower()).strip("_")
    return f"knowledge_chunks_semantic_{model_part}_{EMBEDDING_DIM}"


COLLECTION_NAME = _collection_name()


class _HashEmbeddingFunction:
    """Fallback embedding so local development still runs without API config."""

    def __call__(self, texts: list[str]) -> list[list[float]]:
        result = []
        for text in texts:
            seed = int(hashlib.md5(text.encode("utf-8")).hexdigest()[:8], 16)
            rng = np.random.RandomState(seed)
            vec = rng.randn(EMBEDDING_DIM).astype(np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec /= norm
            result.append(vec.tolist())
        return result


class _OpenAICompatibleEmbeddingFunction:
    def __call__(self, texts: list[str]) -> list[list[float]]:
        if not EMBEDDING_BASE_URL or not EMBEDDING_API_KEY:
            return _hash_embedding_fn(texts)

        base_url = EMBEDDING_BASE_URL.rstrip("/")
        url = f"{base_url}/embeddings" if base_url.endswith("/v1") else f"{base_url}/v1/embeddings"
        headers = {
            "Authorization": f"Bearer {EMBEDDING_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": EMBEDDING_MODEL,
            "input": list(texts),
            "dimensions": EMBEDDING_DIM,
        }
        try:
            with httpx.Client(timeout=60) as client:
                response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            vectors = sorted(data.get("data", []), key=lambda item: item.get("index", 0))
            embeddings = [item.get("embedding", []) for item in vectors]
            if len(embeddings) != len(texts) or not all(embeddings):
                raise ValueError("embedding response shape invalid")
            return embeddings
        except Exception as exc:
            logger.warning("Embedding API failed; using hash fallback: %s", exc)
            return _hash_embedding_fn(texts)


_hash_embedding_fn = _HashEmbeddingFunction()
_embedding_fn = _OpenAICompatibleEmbeddingFunction()


def _client_kwargs() -> dict:
    kwargs = {
        "uri": MILVUS_URI,
    }
    if MILVUS_TOKEN:
        kwargs["token"] = MILVUS_TOKEN
    else:
        if MILVUS_USER:
            kwargs["user"] = MILVUS_USER
        if MILVUS_PASSWORD:
            kwargs["password"] = MILVUS_PASSWORD
    if MILVUS_DB_NAME and not _is_lite_uri(MILVUS_URI):
        kwargs["db_name"] = MILVUS_DB_NAME
    return kwargs


def _connect_milvus() -> MilvusClient:
    global _client
    if _client is None:
        _client = MilvusClient(**_client_kwargs())
    return _client


def _collection_schema() -> CollectionSchema:
    fields = [
        FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, auto_id=False, max_length=256),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=EMBEDDING_DIM),
        FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=65535),
        FieldSchema(name="file_id", dtype=DataType.INT64),
        FieldSchema(name="file_name", dtype=DataType.VARCHAR, max_length=1024),
        FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, max_length=128),
        FieldSchema(name="knowledge_base_id", dtype=DataType.INT64),
    ]
    return CollectionSchema(fields, description="Enterprise knowledge base chunks", enable_dynamic_field=False)


def _index_params():
    index_params = MilvusClient.prepare_index_params()
    index_params.add_index(field_name="embedding", index_type="AUTOINDEX", metric_type="COSINE")
    return index_params


def _ensure_collection() -> MilvusClient:
    client = _connect_milvus()
    if not client.has_collection(COLLECTION_NAME):
        client.create_collection(
            collection_name=COLLECTION_NAME,
            schema=_collection_schema(),
            index_params=_index_params(),
        )
    client.load_collection(COLLECTION_NAME)
    return client


def embedding_backend_status() -> dict:
    configured = bool(EMBEDDING_BASE_URL and EMBEDDING_API_KEY)
    return {
        "configured": configured,
        "model": EMBEDDING_MODEL,
        "dimension": EMBEDDING_DIM,
        "mode": "openai-compatible" if configured else "hash-fallback",
        "vector_store": "milvus_lite" if _is_lite_uri(MILVUS_URI) else "milvus",
        "milvus_configured": bool(MILVUS_URI),
        "milvus_uri": MILVUS_URI,
        "collection": COLLECTION_NAME,
    }


def add_chunks(chunks: list[dict], file_id: int, file_name: str, knowledge_base_id: int):
    client = _ensure_collection()
    _delete_file_chunks(client, file_id)
    if not chunks:
        logger.info("Milvus replace completed: file_id=%s chunks_count=0 action=replace_empty", file_id)
        return
    documents = [chunk["text"] for chunk in chunks]
    embeddings = _embedding_fn(documents)
    rows = [
        {
            "id": f"{file_id}_{chunk['id']}",
            "embedding": embedding,
            "content": text,
            "file_id": int(file_id),
            "file_name": file_name,
            "chunk_id": str(chunk["id"]),
            "knowledge_base_id": int(knowledge_base_id),
        }
        for chunk, embedding, text in zip(chunks, embeddings, documents, strict=True)
    ]
    client.insert(collection_name=COLLECTION_NAME, data=rows)
    logger.info(
        "Milvus replace completed: file_id=%s chunks_count=%s knowledge_base_id=%s action=replace",
        file_id,
        len(chunks),
        knowledge_base_id,
    )


def _delete_file_chunks(client: MilvusClient, file_id: int) -> None:
    if not client.has_collection(COLLECTION_NAME):
        logger.info("Milvus delete skipped: file_id=%s chunks_count=0 action=none", file_id)
        return
    client.delete(collection_name=COLLECTION_NAME, filter=f"file_id == {int(file_id)}")


def delete_file_chunks(file_id: int):
    client = _connect_milvus()
    _delete_file_chunks(client, file_id)
    logger.info("Milvus delete completed: file_id=%s action=delete", file_id)


def query_vectors(
    query: str,
    top_k: int = RETRIEVAL_ROUTE_TOP_K,
    knowledge_base_id: Optional[int] = None,
    route: str = "vector",
) -> list[dict]:
    if not str(query or "").strip():
        return []

    client = _connect_milvus()
    if not client.has_collection(COLLECTION_NAME):
        return []
    client.load_collection(COLLECTION_NAME)
    query_embedding = _embedding_fn([query])[0]
    filter_expr = f"knowledge_base_id == {int(knowledge_base_id)}" if knowledge_base_id is not None else ""
    results = client.search(
        collection_name=COLLECTION_NAME,
        data=[query_embedding],
        anns_field="embedding",
        search_params={"metric_type": "COSINE"},
        limit=min(max(top_k, 1), 20),
        filter=filter_expr,
        output_fields=["content", "file_id", "file_name", "chunk_id", "knowledge_base_id"],
    )
    if not results:
        return []

    chunks = []
    for hit in results[0]:
        hit_data = _normalize_hit(hit)
        chunks.append(
            {
                "id": str(hit_data.get("id", "")),
                "chunk_id": str(hit_data.get("chunk_id", "")),
                "content": str(hit_data.get("content", "")),
                "file_name": str(hit_data.get("file_name", "")),
                "file_id": _safe_int(hit_data.get("file_id")),
                "route": route,
                "distance": hit_data.get("distance"),
            }
        )
    return chunks


def _normalize_hit(hit) -> dict:
    if isinstance(hit, dict):
        entity = hit.get("entity")
        if isinstance(entity, dict):
            data = dict(entity)
        else:
            data = dict(hit)
        data.setdefault("id", hit.get("id"))
        data.setdefault("distance", hit.get("distance"))
        return data

    data = {}
    for key in ("id", "distance"):
        if hasattr(hit, key):
            data[key] = getattr(hit, key)
    entity = getattr(hit, "entity", None)
    if entity is not None:
        if isinstance(entity, dict):
            data.update(entity)
        else:
            for key in ("content", "file_id", "file_name", "chunk_id", "knowledge_base_id"):
                if hasattr(entity, key):
                    data[key] = getattr(entity, key)
                elif hasattr(entity, "get"):
                    try:
                        value = entity.get(key)
                    except Exception:
                        value = None
                    if value is not None:
                        data[key] = value
    return data


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
