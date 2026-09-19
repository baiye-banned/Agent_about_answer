import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from rag import milvus_client


@pytest.fixture(autouse=True)
def _reset_embedding_state():
    """向量来源状态是进程内全局，用例前后都清空，避免用例之间相互影响。"""

    def reset():
        state = getattr(milvus_client, "_embedding_state", None)
        if state is None:
            # 状态缺失时跳过重置，让用例自身的断言去暴露问题。
            return
        state.update(source="", last_error="", last_error_at="", last_used_at="")

    reset()
    yield
    reset()


@contextlib.contextmanager
def _embedding_server(status_code: int, payload: dict):
    """本地上游桩：模拟向量化接口，默认返回指定状态码与 JSON。"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length") or 0))
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_lite_uri_is_file_path_not_server_uri():
    assert milvus_client._is_lite_uri("./milvus.db") is True
    assert milvus_client._is_lite_uri("http://127.0.0.1:19530") is False


def test_restore_milvus_uri_env_round_trip(monkeypatch):
    monkeypatch.setattr(milvus_client, "MILVUS_URI", "./milvus.db")
    monkeypatch.setenv("MILVUS_URI", "./milvus.db")

    hidden = milvus_client._hide_lite_uri_from_pymilvus_import()
    assert hidden == "./milvus.db"

    milvus_client._restore_milvus_uri_env(hidden)
    assert milvus_client.os.environ["MILVUS_URI"] == "./milvus.db"


def test_add_chunks_replaces_existing_file_chunks(monkeypatch):
    calls = []

    class FakeClient:
        def has_collection(self, collection_name):
            return True

        def delete(self, collection_name, filter):
            calls.append(("delete", collection_name, filter))

        def insert(self, collection_name, data):
            calls.append(("insert", collection_name, data))

    def fake_embedding(texts):
        return [[0.1, 0.2] for _ in texts]

    monkeypatch.setattr(milvus_client, "_ensure_collection", lambda: FakeClient())
    monkeypatch.setattr(milvus_client, "_embedding_fn", fake_embedding)

    milvus_client.add_chunks(
        [{"id": "0", "text": "第一段"}, {"id": "1", "text": "第二段"}],
        file_id=7,
        file_name="制度.txt",
        knowledge_base_id=3,
    )

    assert calls[0] == ("delete", milvus_client.COLLECTION_NAME, "file_id == 7")
    assert calls[1][0] == "insert"
    assert [row["id"] for row in calls[1][2]] == ["7_0", "7_1"]


def test_add_chunks_empty_list_still_clears_existing_file_chunks(monkeypatch):
    calls = []

    class FakeClient:
        def has_collection(self, collection_name):
            return True

        def delete(self, collection_name, filter):
            calls.append(("delete", collection_name, filter))

        def insert(self, collection_name, data):
            calls.append(("insert", collection_name, data))

    monkeypatch.setattr(milvus_client, "_ensure_collection", lambda: FakeClient())

    milvus_client.add_chunks([], file_id=9, file_name="空文件.txt", knowledge_base_id=3)

    assert calls == [("delete", milvus_client.COLLECTION_NAME, "file_id == 9")]


def test_query_vectors_empty_query_returns_empty_without_connecting(monkeypatch):
    def fail_connect():
        raise AssertionError("empty query should not connect to Milvus")

    monkeypatch.setattr(milvus_client, "_connect_milvus", fail_connect)

    assert milvus_client.query_vectors("   ", knowledge_base_id=3) == []


def test_query_vectors_normalizes_invalid_hit_file_id(monkeypatch):
    class FakeClient:
        def has_collection(self, collection_name):
            return True

        def load_collection(self, collection_name):
            return None

        def search(self, **kwargs):
            return [
                [
                    {
                        "id": "hit-1",
                        "distance": 0.2,
                        "entity": {
                            "content": "chunk",
                            "file_id": "bad-file-id",
                            "file_name": "制度.txt",
                            "chunk_id": "1",
                        },
                    }
                ]
            ]

    monkeypatch.setattr(milvus_client, "_connect_milvus", lambda: FakeClient())
    monkeypatch.setattr(milvus_client, "_embedding_fn", lambda texts: [[0.1, 0.2] for _ in texts])

    chunks = milvus_client.query_vectors("query", knowledge_base_id=3)

    assert chunks[0]["file_id"] == 0


class _FakeHttpClient:
    """模拟 httpx.Client 上下文管理器，返回固定 JSON 响应。"""

    def __init__(self, status_code=200, payload=None, error=None, url_sink=None):
        self._status_code = status_code
        self._payload = payload
        self._error = error
        self._url_sink = url_sink

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url, json=None, headers=None):
        if self._url_sink is not None:
            self._url_sink.append(url)
        if self._error is not None:
            raise self._error
        response = httpx.Response(self._status_code, json=self._payload, request=httpx.Request("POST", url))
        return response


def _patch_configured_embedding(monkeypatch, status_code=200, payload=None, error=None):
    monkeypatch.setattr(milvus_client, "EMBEDDING_BASE_URL", "https://embedding.example/v1")
    monkeypatch.setattr(milvus_client, "EMBEDDING_API_KEY", "test-key")
    monkeypatch.setattr(
        milvus_client.httpx,
        "Client",
        lambda *args, **kwargs: _FakeHttpClient(status_code=status_code, payload=payload, error=error),
    )


def test_configured_embedding_api_429_raises_instead_of_hash_fallback(monkeypatch):
    with _embedding_server(429, {"error": {"message": "Requests rate limit exceeded"}}) as base_url:
        monkeypatch.setattr(milvus_client, "EMBEDDING_BASE_URL", base_url)
        monkeypatch.setattr(milvus_client, "EMBEDDING_API_KEY", "test-key")

        with pytest.raises(milvus_client.EmbeddingBackendError) as exc_info:
            milvus_client._OpenAICompatibleEmbeddingFunction()(["hello"])

    message = str(exc_info.value)
    assert "向量化接口调用失败" in message
    assert "429" in message

    status = milvus_client.embedding_backend_status()
    assert status["configured"] is True
    assert status["mode"] == "unavailable"
    assert "429" in status["last_error"]
    assert status["last_error_at"]
    assert status["last_used_at"] == ""


def test_configured_embedding_request_error_raises(monkeypatch):
    _patch_configured_embedding(monkeypatch, error=RuntimeError("connection reset"))

    with pytest.raises(milvus_client.EmbeddingBackendError) as exc_info:
        milvus_client._OpenAICompatibleEmbeddingFunction()(["hello"])

    assert "connection reset" in str(exc_info.value)
    assert milvus_client.embedding_backend_status()["mode"] == "unavailable"


def test_configured_embedding_invalid_response_shape_raises(monkeypatch):
    _patch_configured_embedding(monkeypatch, payload={"data": []})

    with pytest.raises(milvus_client.EmbeddingBackendError) as exc_info:
        milvus_client._OpenAICompatibleEmbeddingFunction()(["hello"])

    assert "响应结构异常" in str(exc_info.value)


def test_configured_embedding_success_reports_openai_mode(monkeypatch):
    urls = []
    monkeypatch.setattr(milvus_client, "EMBEDDING_BASE_URL", "https://embedding.example/v1")
    monkeypatch.setattr(milvus_client, "EMBEDDING_API_KEY", "test-key")
    monkeypatch.setattr(
        milvus_client.httpx,
        "Client",
        lambda *args, **kwargs: _FakeHttpClient(
            payload={"data": [{"index": 0, "embedding": [0.1, 0.2]}]},
            url_sink=urls,
        ),
    )

    vectors = milvus_client._OpenAICompatibleEmbeddingFunction()(["hello"])

    assert vectors == [[0.1, 0.2]]
    assert urls == ["https://embedding.example/v1/embeddings"]
    status = milvus_client.embedding_backend_status()
    assert status["mode"] == "openai-compatible"
    assert status["last_error"] == ""
    assert status["last_used_at"]


def test_unconfigured_embedding_keeps_hash_fallback_and_reports_hash_mode(monkeypatch):
    monkeypatch.setattr(milvus_client, "EMBEDDING_BASE_URL", "")
    monkeypatch.setattr(milvus_client, "EMBEDDING_API_KEY", "")

    vectors = milvus_client._OpenAICompatibleEmbeddingFunction()(["hello"])

    assert len(vectors) == 1
    assert len(vectors[0]) == milvus_client.EMBEDDING_DIM
    status = milvus_client.embedding_backend_status()
    assert status["configured"] is False
    assert status["mode"] == "hash-fallback"
    assert status["last_error"] == ""
    assert status["last_used_at"]


def test_add_chunks_embedding_failure_keeps_existing_index_untouched(monkeypatch):
    calls = []

    class FakeClient:
        def has_collection(self, collection_name):
            return True

        def delete(self, collection_name, filter):
            calls.append(("delete", collection_name, filter))

        def insert(self, collection_name, data):
            calls.append(("insert", collection_name, data))

    def failing_embedding(texts):
        raise milvus_client.EmbeddingBackendError("向量化接口调用失败（https://embedding.example/v1/embeddings）：429 Too Many Requests")

    monkeypatch.setattr(milvus_client, "_ensure_collection", lambda: FakeClient())
    monkeypatch.setattr(milvus_client, "_embedding_fn", failing_embedding)

    with pytest.raises(milvus_client.EmbeddingBackendError):
        milvus_client.add_chunks(
            [{"id": "0", "text": "第一段"}, {"id": "1", "text": "第二段"}],
            file_id=7,
            file_name="制度.txt",
            knowledge_base_id=3,
        )

    assert calls == []


def test_query_vectors_embedding_failure_raises_before_search(monkeypatch):
    search_calls = []

    class FakeClient:
        def has_collection(self, collection_name):
            return True

        def load_collection(self, collection_name):
            return None

        def search(self, **kwargs):
            search_calls.append(kwargs)
            return [[]]

    def failing_embedding(texts):
        raise milvus_client.EmbeddingBackendError("向量化接口调用失败：429 Too Many Requests")

    monkeypatch.setattr(milvus_client, "_connect_milvus", lambda: FakeClient())
    monkeypatch.setattr(milvus_client, "_embedding_fn", failing_embedding)

    with pytest.raises(milvus_client.EmbeddingBackendError):
        milvus_client.query_vectors("迟到怎么处罚", knowledge_base_id=3)

    assert search_calls == []
