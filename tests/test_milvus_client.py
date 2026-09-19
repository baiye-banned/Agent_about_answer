import httpx

from rag import milvus_client


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


def test_embedding_default_timeout_is_online_qa_scale():
    """默认超时必须收敛到线上问答量级，否则一轮 9 路召回会被单次调用拖住。"""
    assert 0 < milvus_client.EMBEDDING_TIMEOUT_SECONDS <= 10


def test_embedding_client_uses_configured_timeout(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"index": 0, "embedding": [0.1] * milvus_client.EMBEDDING_DIM}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(milvus_client, "EMBEDDING_BASE_URL", "https://embedding.example/v1")
    monkeypatch.setattr(milvus_client, "EMBEDDING_API_KEY", "test-key")
    monkeypatch.setattr(milvus_client, "EMBEDDING_TIMEOUT_SECONDS", 7)
    monkeypatch.setattr(milvus_client.httpx, "Client", FakeClient)

    vectors = milvus_client._OpenAICompatibleEmbeddingFunction()(["hello"])

    assert captured["timeout"] == 7
    assert len(vectors) == 1


def test_embedding_timeout_does_not_leak_raw_transport_error(monkeypatch):
    """向量化接口超时不能以 httpx 原始异常的形式泄漏给检索链路。

    断言同时兼容“降级为本地兜底向量”和“抛出明确的向量化后端错误”两种策略，
    避免与并行改造向量化失败语义的分支互相冲突。
    """

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            raise httpx.TimeoutException("embedding request timed out")

    monkeypatch.setattr(milvus_client, "EMBEDDING_BASE_URL", "https://embedding.example/v1")
    monkeypatch.setattr(milvus_client, "EMBEDDING_API_KEY", "test-key")
    monkeypatch.setattr(milvus_client, "EMBEDDING_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(milvus_client.httpx, "Client", FakeClient)

    try:
        vectors = milvus_client._OpenAICompatibleEmbeddingFunction()(["hello"])
    except httpx.HTTPError as exc:
        raise AssertionError(f"向量化超时以原始 httpx 异常泄漏：{exc!r}")
    except Exception as exc:
        assert str(exc)
    else:
        assert len(vectors) == 1


def test_embedding_fallback_logs_warning(monkeypatch, caplog):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            raise RuntimeError("embedding unavailable")

    monkeypatch.setattr(milvus_client, "EMBEDDING_BASE_URL", "https://embedding.example/v1")
    monkeypatch.setattr(milvus_client, "EMBEDDING_API_KEY", "test-key")
    monkeypatch.setattr(milvus_client.httpx, "Client", FakeClient)

    with caplog.at_level("WARNING", logger=milvus_client.__name__):
        vectors = milvus_client._OpenAICompatibleEmbeddingFunction()(["hello"])

    assert len(vectors) == 1
    assert "Embedding API failed; using hash fallback" in caplog.text
