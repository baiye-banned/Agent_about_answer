# Test Baseline

This directory contains the executable regression baseline for the current RAG maintenance phase.

## Node Tests

Run from the repository root:

```powershell
npm test
```

The test script uses `tests/**/*.test.js`, so future nested Node test files that
end in `.test.js` are included as well.

Current files:

- `chatApi.test.js`
- `chatStore.test.js`
- `clipboard.test.js`
- `knowledgeFeedback.test.js`
- `streamEvents.test.js`
- `url.test.js`
- `utils.test.js`

These tests cover frontend stream parsing, stream response errors, chat store conversation switching, stale in-flight response handling and fallback reset events, clipboard fallback behavior, knowledge batch delete and upload feedback helpers, URL normalization and link safety, display formatting, status helpers, image validation, and memory trace helpers.

## Python Tests

Run from the repository root:

```powershell
python -m pytest -q tests
```

Discovery is fixed by the repository `pytest.ini`: `testpaths = tests` and
`python_files = test_*.py`.

Current files:

- `test_auth_service.py`
- `test_boundary_modules.py`
- `test_chat_service_retrieval.py`
- `test_checkpointer.py`
- `test_chunking.py`
- `test_config_helpers.py`
- `test_default_users.py`
- `test_env_example_parity.py`
- `test_grounding.py`
- `test_json_utils.py`
- `test_knowledge_ownership.py`
- `test_knowledge_service.py`
- `test_knowledge_upload_limits.py`
- `test_learning_trace.py`
- `test_llm_urls.py`
- `test_memory_context.py`
- `test_milvus_acceptance.py`
- `test_milvus_client.py`
- `test_provider_smoke.py`
- `test_ragas_eval.py`
- `test_rerank.py`
- `test_retrieval.py`
- `test_retrieval_acceptance.py`
- `test_secret_key_guard.py`
- `test_stream_fallback_reset.py`
- `test_trace_crud.py`
- `test_upload_validation.py`
- `conftest.py`

These tests cover retrieval planning and fusion, chat service retrieval wiring, auth token validation, boundary modules that had no test reference before (trace SSE framing and the safe-trace wrappers, RAG generation argument forwarding and laziness, OSS host/signing/PUT requests, and vision question construction with image analysis classification), default user seeding without hardcoded passwords, `.env.example`/`config.py` parity, checkpointer helpers, semantic chunking, config parsing, grounding helpers, JSON loading, knowledge ownership filtering, knowledge deletion ordering, knowledge upload type and size limits, learning trace handling, OpenAI-compatible URL helpers, memory context, Milvus client behavior, RAGAS text handling, rerank fallback, SECRET_KEY startup guard, stream fallback reset handling, trace CRUD, and upload validation.

`conftest.py` puts `backend/` on `sys.path` so the tests can import application modules, and holds the test doubles shared by more than one test file: the `FakeQuery`/`FakeDb`/`FakeUser`/`FakeKnowledgeBase`/`FakeTraceRecorder` classes, the pytest fixtures built on them (`fake_user`, `fake_db`, `fake_knowledge_base`, `trace_recorder_cls`), and the SSE helpers (`collect_stream`, `parse_sse_frames`, `frames_of_type`, `streamed_content`). Test doubles used by a single file stay in that file.

`test_milvus_acceptance.py`, `test_retrieval_acceptance.py` and `test_provider_smoke.py` are the behavioral acceptance layer for the maintenance goals in `docs/MAINTENANCE_GOAL_CLOSURE.md`: a real Milvus Lite round trip (upload, query, delete, knowledge-base isolation, index rebuild), the full `retrieve_knowledge` chain (multi-route recall, RRF fusion, rerank, final context order), and the provider clients (DeepSeek, embedding, rerank and their fallbacks) including failure, timeout and malformed-response paths. They need no network and no credentials.

The provider smoke entry point is `scripts/smoke_providers.py`; it runs offline against in-process stubs by default and only reaches the real endpoints with `--live`.

## Excluded Local Files

Do not stage generated or design-only files:

- `__pycache__/`
- `*.pyc`
- `.pytest_cache/`
- `tests/.test.md`
- `tests/*.test.md`

The ignore rules are intentionally kept in the repository `.gitignore` so the executable test baseline can be staged without cache files.
