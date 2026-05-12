import hashlib
import logging
from typing import Optional

import chromadb
import httpx
import numpy as np
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from chromadb.config import Settings

from config import (
    CHROMA_PERSIST_DIR,
    EMBEDDING_API_KEY,
    EMBEDDING_BASE_URL,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    RETRIEVAL_ROUTE_TOP_K,
)

_client = None
_semantic_collection = None
logger = logging.getLogger(__name__)

LEGACY_COLLECTION_NAME = "knowledge_chunks"


def _collection_name() -> str:
    model_part = "".join(ch if ch.isalnum() else "_" for ch in EMBEDDING_MODEL.lower()).strip("_")
    return f"knowledge_chunks_semantic_{model_part}_{EMBEDDING_DIM}"


COLLECTION_NAME = _collection_name()

try:
    import posthog as _posthog  # type: ignore
except Exception:  # pragma: no cover - optional dependency behavior only
    _posthog = None


class _HashEmbeddingFunction(EmbeddingFunction):
    """Fallback embedding so local development still runs without API config."""

    def name(self):
        return "hash_embedding_fallback"

    def __call__(self, texts: Documents) -> Embeddings:
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


class _OpenAICompatibleEmbeddingFunction(EmbeddingFunction):
    def name(self):
        return "openai_compatible_embedding"

    def __call__(self, texts: Documents) -> Embeddings:
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
        except Exception:
            return _hash_embedding_fn(texts)


_hash_embedding_fn = _HashEmbeddingFunction()
_embedding_fn = _OpenAICompatibleEmbeddingFunction()


def _suppress_chroma_telemetry_noise():
    logging.getLogger("chromadb.telemetry.product.posthog").setLevel(logging.ERROR)
    logging.getLogger("posthog").setLevel(logging.ERROR)
    if _posthog is not None:
        try:
            _posthog.disabled = True
            _posthog.capture = lambda *args, **kwargs: None
        except Exception:
            pass


_suppress_chroma_telemetry_noise()


def embedding_backend_status() -> dict:
    configured = bool(EMBEDDING_BASE_URL and EMBEDDING_API_KEY)
    return {
        "configured": configured,
        "model": EMBEDDING_MODEL,
        "dimension": EMBEDDING_DIM,
        "mode": "openai-compatible" if configured else "hash-fallback",
    }


def get_chroma_client():
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(
            path=CHROMA_PERSIST_DIR,
            settings=Settings(anonymized_telemetry=False),
        )
    return _client


def get_collection():
    global _semantic_collection
    if _semantic_collection is None:
        client = get_chroma_client()
        _semantic_collection = client.get_or_create_collection(
            COLLECTION_NAME,
            embedding_function=_embedding_fn,
        )
    return _semantic_collection


def add_chunks(chunks: list[dict], file_id: int, file_name: str, knowledge_base_id: int):
    if not chunks:
        logger.info("Chroma upsert skipped: file_id=%s chunks_count=0 action=none", file_id)
        return
    collection = get_collection()
    ids = [f"{file_id}_{chunk['id']}" for chunk in chunks]
    documents = [chunk["text"] for chunk in chunks]
    metadatas = [
        {
            "chunk_id": str(chunk["id"]),
            "file_id": file_id,
            "file_name": file_name,
            "knowledge_base_id": knowledge_base_id,
        }
        for chunk in chunks
    ]
    collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
    logger.info(
        "Chroma upsert completed: file_id=%s chunks_count=%s knowledge_base_id=%s action=upsert",
        file_id,
        len(ids),
        knowledge_base_id,
    )


def delete_file_chunks(file_id: int):
    collection = get_collection()
    results = collection.get(where={"file_id": file_id})
    ids = results.get("ids") or []
    if not ids:
        logger.info("Chroma delete skipped: file_id=%s chunks_count=0 action=none", file_id)
        return
    collection.delete(where={"file_id": file_id})
    logger.info(
        "Chroma delete completed: file_id=%s chunks_count=%s action=delete",
        file_id,
        len(ids),
    )


def query_vectors(
    query: str,
    top_k: int = RETRIEVAL_ROUTE_TOP_K,
    knowledge_base_id: Optional[int] = None,
    route: str = "vector",
) -> list[dict]:
    collection = get_collection()
    where = {"knowledge_base_id": knowledge_base_id} if knowledge_base_id else None
    results = collection.query(
        query_texts=[query],
        n_results=min(max(top_k, 1), 20),
        where=where,
    )
    if not results["documents"] or not results["documents"][0]:
        return []

    distances = results.get("distances") or [[]]
    chunks = []
    for index, doc in enumerate(results["documents"][0]):
        meta = results["metadatas"][0][index] if results["metadatas"] else {}
        distance = distances[0][index] if distances and len(distances[0]) > index else None
        chunks.append({
            "id": results["ids"][0][index],
            "chunk_id": str(meta.get("chunk_id", "")),
            "content": doc,
            "file_name": meta.get("file_name", ""),
            "file_id": meta.get("file_id", 0),
            "route": route,
            "distance": distance,
        })
    return chunks


def search_knowledge(query: str, top_k: int = 3, knowledge_base_id: int | None = None):
    chunks = query_vectors(query, top_k=top_k, knowledge_base_id=knowledge_base_id, route="legacy_vector")
    return [
        {
            "content": chunk["content"],
            "file_name": chunk["file_name"],
            "file_id": chunk["file_id"],
        }
        for chunk in chunks
    ]
