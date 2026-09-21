# Test Baseline

This directory contains the executable regression baseline for the current RAG maintenance phase.

**When you add a test file, register it in the two lists below in the same PR.** Both lists are
hand-maintained: `npm test` discovers files through `tests/**/*.test.js` and pytest through
`python_files = test_*.py` in `pytest.ini`, so a missing entry never stops a test from running - it
only makes that file invisible to anyone auditing coverage against this document, which is how an
already-covered behaviour ends up being read as uncovered. The directory itself is the source of
truth; when a list and the tree disagree, `ls tests/*.test.js tests/test_*.py tests/conftest.py`
and `python -m pytest tests --collect-only` win. (`tests/e2e/*.spec.mjs` is the Playwright suite
run by `npm run test:e2e` and is deliberately in neither list.)

## Node Tests

Requires Node.js **>= 22.15**. `chatStore.test.js` imports `module.registerHooks`, which Node
documents as *Added in: v23.5.0, v22.15.0*; on Node 18/20 and 22.0-22.14 that import fails and
`node --test` counts the file as failed, so the whole `npm test` run exits non-zero - not just
that one file. The 23.x line needs **23.5+** (23.0.0-23.4.x lacks the export); 24+ is fine.

The floor is declared in `engines.node` (`>=22.15`) in `package.json` and in the environment
list in `README.md`; keep the three in sync. `npm install` / `npm ci` prints an `EBADENGINE`
warning when the running Node does not satisfy `engines.node`. Because `>=22.15` is a plain
floor it also admits the EOL 23.0.0-23.4.x releases, which this check rejects:

```bash
node -e "const [M,m]=process.versions.node.split('.').map(Number); process.exit((M===22&&m>=15)||(M===23&&m>=5)||M>=24?0:1)"
```

The workflows in `.github/workflows/` pin `node-version: "22"`, which resolves to the latest
22.x and therefore sits above the floor - nothing in CI runs at 22.15.0 itself. To cover the
floor locally, pin it with any version manager and run the suite from the repository root:

```bash
nvm install 22.15.0 && nvm use 22.15.0    # or fnm/asdf with the same version
npm ci && npm test                        # exits 0
nvm use 22.14.0 && npm test               # exits 1: chatStore.test.js fails to load
```

Re-run this whenever `engines.node` or the tests' API usage changes; 22.14.0 is the cheap
half of the check, since a floor that is too low only shows up on the version below it.

Run from the repository root:

```powershell
npm test
```

The test script uses `tests/**/*.test.js`, so future nested Node test files that
end in `.test.js` are included as well.

Current files:

- `chatApi.test.js`
- `chatStore.test.js`
- `ciGateScripts.test.js`
- `clipboard.test.js`
- `detailPreview.test.js`
- `fileListRequest.test.js`
- `knowledgeFeedback.test.js`
- `knowledgeUploadCallSiteMount.test.js`
- `knowledgeUploadTypes.test.js`
- `knowledgeViewWiring.test.js`
- `markdownSanitize.test.js`
- `streamEvents.test.js`
- `traceVariableFlowMount.test.js`
- `url.test.js`
- `utils.test.js`

These tests cover frontend stream parsing, stream response errors, chat store conversation switching, stale in-flight response handling and fallback reset events, knowledge file detail preview ordering (a late response from a previously opened file must not rewrite the current one, and closing the dialog discards in-flight requests), clipboard fallback behavior, knowledge batch delete and upload feedback helpers, the shared delete confirmation orchestration (user cancel, API rejection, and success outcomes), the upload-type whitelist staying in sync with the backend's accepted extensions (a static cross-check against `backend/service/utils_service.py`), HTML sanitization of rendered markdown (script/style/event-handler stripping, the element allowlist, and disallowed elements being dropped together with their content), URL normalization and link safety, display formatting, status helpers, image validation, the CI gate scripts (`scripts/check_issue.mjs`, `scripts/check_pr_body.mjs` and their shared `scripts/lib/markdown_sanitize.mjs`: the comment sanitizer's invariants - no comment start marker left behind, no section heading deleted by an unpaired marker - plus both gates' pass/fail verdicts on the fixtures in `tests/fixtures/ci-gate/`, including the code-fence behaviour that is deliberate rather than an oversight), memory trace helpers, and the mounted-SFC layer for `TraceVariableFlow.vue` (`traceVariableFlowMount.test.js`: `@vue/compiler-sfc` compiles the real SFC, jsdom hosts it and Element Plus supplies the real `el-switch`, so the assertions cover the main-line/branch classification, the only-main-line and expand-branch switch linkage including the reset of the expand state, the variable inspector's selection, same-name upstream/downstream wiring and full-value rendering, and the clipboard toast paths - see the file header for which of the component's two state-sync watchers is reachable from the UI and which one is not). The mounted-SFC layer also covers `Knowledge.vue`'s upload entry (`knowledgeUploadCallSiteMount.test.js`: a real `change` event on the hidden file input drives the whole chain, and the assertions pin the complete `ElMessage` array rather than a subset, so an upload whose follow-up list refresh fails keeps the success toast and adds only a refresh-failure message, while a genuine upload failure still reports as one).

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
- `test_chunk_key_namespace.py`
- `test_chunking.py`
- `test_config_helpers.py`
- `test_default_users.py`
- `test_env_example_parity.py`
- `test_grounding.py`
- `test_json_utils.py`
- `test_knowledge_base_name_race.py`
- `test_knowledge_ownership.py`
- `test_knowledge_service.py`
- `test_knowledge_upload_limits.py`
- `test_learning_trace.py`
- `test_list_query_counts.py`
- `test_llm_urls.py`
- `test_memory_context.py`
- `test_milvus_acceptance.py`
- `test_milvus_client.py`
- `test_orphan_attachment_cleanup_142.py`
- `test_provider_smoke.py`
- `test_ragas_eval.py`
- `test_rerank.py`
- `test_retrieval.py`
- `test_retrieval_acceptance.py`
- `test_scan_secrets_selftest.py`
- `test_secret_key_guard.py`
- `test_sse_session_leak.py`
- `test_stream_fallback_reset.py`
- `test_trace_crud.py`
- `test_upload_validation.py`
- `conftest.py`

These tests cover retrieval planning and fusion, chat service retrieval wiring, auth token validation, boundary modules that had no test reference before (trace SSE framing and the safe-trace wrappers, RAG generation argument forwarding and laziness, OSS host/signing/PUT requests, and vision question construction with image analysis classification), default user seeding without hardcoded passwords, `.env.example`/`config.py` parity, checkpointer helpers, semantic chunking, chunk-id namespace isolation between keyword windows and the stored slices, config parsing, grounding helpers, JSON loading, list-endpoint SQL query-count bounds and message-history pagination, knowledge base name race handling (concurrent duplicate-name create/rename, session rollback and the global IntegrityError fallback), knowledge ownership filtering, knowledge deletion ordering, knowledge upload type and size limits, learning trace handling, OpenAI-compatible URL helpers, memory context, Milvus client behavior, RAGAS text handling, rerank fallback, SECRET_KEY startup guard, SSE client-disconnect session cleanup (a client that goes away must still close the DB session and return the connection to the pool), the built-in secret-scan rule self-test (all five patterns, the exemption-marker boundary - including a dual-path fixture proving the marker is really consulted for the assignment hit while still failing to exempt the `sk-` shape hit on the same line - and a mutation check that neutering any single rule stops its sample from being reported), stream fallback reset handling, trace CRUD, and upload validation.

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
