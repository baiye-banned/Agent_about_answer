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
- `clipboard.test.js`
- `streamEvents.test.js`
- `utils.test.js`

These tests cover frontend stream parsing, stream response errors, clipboard fallback behavior, URL normalization, display formatting, status helpers, image validation, and memory trace helpers.

## Python Tests

Run from the repository root:

```powershell
python -m pytest -q tests
```

Discovery is fixed by the repository `pytest.ini`: `testpaths = tests` and
`python_files = test_*.py`.

Current files:

- `test_auth_service.py`
- `test_checkpointer.py`
- `test_chunking.py`
- `test_config_helpers.py`
- `test_grounding.py`
- `test_json_utils.py`
- `test_knowledge_service.py`
- `test_learning_trace.py`
- `test_llm_urls.py`
- `test_memory_context.py`
- `test_milvus_client.py`
- `test_ragas_eval.py`
- `test_rerank.py`
- `test_retrieval.py`
- `test_trace_crud.py`
- `test_upload_validation.py`
- `conftest.py`

These tests cover retrieval planning and fusion, auth token validation, checkpointer helpers, semantic chunking, config parsing, grounding helpers, JSON loading, knowledge deletion ordering, learning trace handling, OpenAI-compatible URL helpers, memory context, Milvus client behavior, RAGAS text handling, rerank fallback, trace CRUD, upload validation, and shared fixtures.

## Excluded Local Files

Do not stage generated or design-only files:

- `__pycache__/`
- `*.pyc`
- `.pytest_cache/`
- `.test.md`
- `*.test.md`

The ignore rules are intentionally kept in the repository `.gitignore` so the executable test baseline can be staged without cache files.
