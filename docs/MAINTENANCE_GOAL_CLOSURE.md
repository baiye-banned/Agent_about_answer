# Maintenance Goal Closure

## Summary

This report minimally closes the current phase of the long-running maintenance goal: continue auditing project gaps and improvement points, while keeping simple logic simple, readable, and maintainable.

The goal ran for about 14 hours and 47 minutes (`53275` seconds) and used about `8,992,476` tokens before this closure pass. This closure intentionally stops adding new optimizations. It records the current work, verification evidence, residual risks, and follow-up goals.

Snapshot baseline:

- Date/time: `2026-06-02 15:39:31 +08:00`
- Branch: `main`
- HEAD: `1a99a9a`
- Worktree state: dirty; this report records the current phase, not a clean release boundary.

## Current Worktree Shape

- `git status --short` showed a large dirty tree at the snapshot: backend, frontend, docs, and config.
- `git diff --stat` reports `45 files changed`, with `1296 insertions` and `2874 deletions` in tracked files.
- Major deletion/replacement areas include old Chroma files and old learning-center trace replay files.
- Major additions in that snapshot were still untracked, including Milvus, rerank, JSON helper, frontend utility modules, and automated tests. These paths, including `tests/`, are tracked now.

## Completed Work Themes

### Frontend

- Split stream response validation and SSE parsing out of `streamChat` into small utilities.
- Extracted repeated UI support logic into utilities for error text, display text, file validation, image analysis status, RAGAS status, memory trace formatting, URL normalization, clipboard copy, confirmations, and chat suggestions.
- Simplified `Chat.vue`, `Knowledge.vue`, `UserProfile.vue`, `Login.vue`, `Layout.vue`, and `TraceVariableFlow.vue` by moving repeated logic into shared helpers.
- Removed the old learning-center trace replay components from the active route. The standalone learning-center page was later retired to keep the frontend surface smaller.

### Backend

- Migrated the vector-store direction from Chroma to Milvus/Milvus Lite.
- Retired the controlled agentic planner path and kept retrieval planning inside `backend/rag/retrieval.py`.
- Split rerank behavior into a dedicated helper with DashScope rerank and fallback behavior.
- Centralized OpenAI-compatible URL helpers and JSON parsing behavior.
- Improved semantic chunking, vector cleanup before SQL deletion, upload validation, auth token validation, and trace/grounding helpers.
- Added shared JSON utilities to preserve falsy values such as `0` and `false`.

### Docs, Config, And Tests

- Updated README and project docs toward the current Milvus, LangChain layer, memory, and RAGAS direction.
- Updated example config and dependency direction for Milvus, rerank, RAGAS, and dotenv support.
- Added a Python and Node regression test net covering planner, Milvus client, rerank, config helpers, LLM URL/JSON parsing, knowledge deletion ordering, chunking, grounding, JSON utilities, auth, trace, memory, RAGAS text handling, upload validation, checkpointer behavior, frontend stream parsing, clipboard, and frontend utility helpers.
- Added `tests/README.md` to define the executable test baseline and separate stageable tests from generated cache/design-only files.

## Verification Evidence

Commands run from the repository root:

| Command | Result |
| --- | --- |
| `npm test` | Passed: `80` Node tests; script discovers `tests/**/*.test.js` |
| `npm run build` | Passed: Vite build completed; retained existing `Chat` chunk > 500 kB warning |
| `python -m pytest -q tests` | `206` passed, `1` failed of `207` collected; discovery fixed by `pytest.ini`. The failure (`test_env_example_parity.py::test_every_env_var_read_by_config_is_documented`) comes from upstream commit `483a070`: `backend/config.py` reads `EMBEDDING_TIMEOUT_SECONDS`, but `.env.example` does not document it |
| `git status --short` | Dirty tree remains; see current worktree shape above |
| `git diff --stat` | `45 files changed`, `1296 insertions`, `2874 deletions` |
| `git diff --check -- src backend package.json tests` | No whitespace errors reported; Git printed LF-to-CRLF working-copy warnings |
| `git check-ignore -v .env .env.development node_modules dist backend/milvus.db backend/uploads backend/checkpointer.db frontend.log backend.log backend.pid` | All listed local env/build/data/log/pid paths are ignored |

## Residual Risks

- Node and Python test discovery have been made explicit in `package.json` and `pytest.ini`; future nested Node tests and Python `test_*.py` files under `tests/` should stay inside the baseline.
- Milvus migration is the largest backend risk. Existing Chroma data is not automatically migrated. The upload/query/delete loop has since had its runtime acceptance pass: `tests/test_milvus_acceptance.py` ran green on `2026-09-22` (`7 passed` in `13.10s` locally) against a real Milvus Lite database created in a temporary directory, covering upload and re-upload replace, cosine query, delete, knowledge-base isolation, index rebuild, and the `top_k` / missing-collection window; the same module is part of the `507 passed` backend suite on the Python 3.10 CI runner. The boundary of that run is embedded Milvus Lite: a deployed Milvus server is still unverified, and the embedding function is a deterministic stand-in, so the real provider path is not covered here.
- Several external providers are mocked in tests. DeepSeek, DashScope embedding, DashScope rerank, OSS, and RAGAS runtime behavior still need real-environment smoke checks.
- Some terminal output showed mojibake for Chinese strings. Prior tests passed, but the final UI text should be checked in a browser or by reading files with confirmed UTF-8 handling.
- `Knowledge.vue` batch delete/upload feedback and `chat store` RAGAS polling/message merge are now covered by narrow behavior-level Node tests (`tests/knowledgeFeedback.test.js`, `tests/chatStore.test.js`). The view-side logic was extracted into `src/utils/knowledgeFeedback.js` to make it testable in plain Node, so the Vue SFC glue itself (which helper it calls, the toast level, `uploading`/`uploadPercent` reset) is still only verified by reading, not executed by tests.

## Follow-Up Small Goals

Immediate next step:

1. Done: the Python/Node test baseline was reviewed and staged, using `tests/README.md` and `.gitignore` to exclude caches and design-only notes.

Next batch:

2. Done: the Milvus acceptance goal verified index rebuild, upload, query, knowledge-base isolation, and deletion cleanup end to end on `2026-09-22`; what remains open is the deployed-server and real-embedding-provider boundary (see [Residual Risks](#residual-risks)).
   - Acceptance tests: `tests/test_milvus_acceptance.py` (see [Acceptance Test Index](#acceptance-test-index)).
3. Retrieval acceptance goal: verify query planning, vector recall, keyword recall, RRF fusion, and rerank with focused behavior tests.
   - Acceptance tests: `tests/test_retrieval_acceptance.py` (see [Acceptance Test Index](#acceptance-test-index)).
4. Rerank and provider goal: smoke-test DashScope rerank, LLM fallback, embedding, and DeepSeek connectivity in the target environment.
   - Acceptance tests: `tests/test_provider_smoke.py` plus the offline smoke entry point `scripts/smoke_providers.py` (`--live` for the real endpoints); see [Acceptance Test Index](#acceptance-test-index).
5. Frontend behavior goal: partially done. Narrow tests for knowledge batch delete/upload feedback and chat RAGAS polling/message merge behavior were added (`tests/knowledgeFeedback.test.js`, `tests/chatStore.test.js`). The three delete entry points in `Knowledge.vue` (delete knowledge base, delete file, batch delete) now share `runConfirmedDelete`, which gives each of them an error branch: cancelling the confirm dialog returns silently instead of raising an unhandled rejection, and a rejected `knowledgeAPI` call emits an error message instead of leaving the view in its old state; `tests/knowledgeFeedback.test.js` covers all three outcomes (cancelled / failed / succeeded). Still open: no test mounts the Vue view, so the untested surface is the call-site glue itself - the `status !== DELETE_SUCCEEDED` short-circuit and the refresh call that follows it.
6. Documentation alignment goal: keep startup commands and environment variables consistent across README, docs, and project instructions.

## Acceptance Test Index

Behavior-level acceptance tests for follow-up goals 2-4, added by issue #22. All of
them run offline and deterministic; no credentials and no network access are required.

### Goal 2 - Milvus acceptance (`tests/test_milvus_acceptance.py`)

Runs against a real Milvus Lite database created in a temporary directory (only the
embedding function is replaced, with a deterministic offline implementation).

- `test_acceptance_upload_query_delete_round_trip` - upload, query with cosine similarity 1, delete, then query again returns nothing.
- `test_acceptance_knowledge_base_isolation` - a query in knowledge base A never sees knowledge base B chunks, while a query without a knowledge base filter sees both.
- `test_acceptance_reupload_replaces_previous_chunks` - re-uploading a file replaces its previous chunks instead of appending.
- `test_acceptance_delete_removes_only_target_file` - deleting one file leaves the other files of the same knowledge base searchable.
- `test_acceptance_index_rebuild_restores_recall_without_duplicates` - `rebuild_existing_knowledge_index()` rebuilds a deleted index with the same chunk count and the same recall result.
- `test_acceptance_query_window_and_missing_collection` - `top_k` window behavior, blank query, missing collection, and a knowledge base with no data.
- `test_acceptance_round_trip_survives_a_dead_proxy_environment` - the round trip stays green in a child process whose proxy variables point at a dead port, so a proxy setting cannot masquerade as a Milvus failure.

### Goal 3 - Retrieval acceptance (`tests/test_retrieval_acceptance.py`)

Drives the full `retrieve_knowledge` chain: multi-route recall, RRF fusion, rerank, and
final context selection. Only the marginal recall and the rerank HTTP transport are
replaced; the real fusion, truncation and selection logic runs. Keyword recall reads the
relational store rather than Milvus, so the last three cases call that implementation
unpatched against real `KnowledgeFile` rows in a real SQLite database.

- `test_acceptance_multi_route_rrf_rerank_final_order` - one path from plan to final context: route order and `top_k`, RRF order and scores, rerank request payload, and a final order that follows the rerank scores.
- `test_acceptance_rerank_failure_falls_back_to_fused_order` - a failed rerank keeps the fused order and reports a failed rerank trace.
- `test_acceptance_rerank_falls_back_to_llm_with_real_fused_candidates` - the LLM fallback receives the real fused candidates and its scores decide the final order.
- `test_acceptance_keyword_boost_prepends_best_keyword_chunk` - a strongly matching keyword chunk is prepended to the final context.
- `test_acceptance_keyword_boost_does_not_duplicate_selected_chunk` - the keyword boost never duplicates an already selected chunk.
- `test_acceptance_empty_recall_skips_rerank_without_provider_call` - an empty recall returns an empty context and never calls the rerank provider.
- `test_acceptance_partial_empty_routes_still_produce_context` - empty routes are reported as count 0 while the remaining route still produces context.
- `test_acceptance_single_route_failure_is_not_swallowed` - a failing recall route aborts the retrieval instead of returning partial context.
- `test_acceptance_route_plan_is_deduplicated_and_capped` - the route plan is deduplicated and capped before recall.
- `test_acceptance_rerank_candidate_window_is_capped` - the candidate window sent to the reranker and the returned context are capped by the configured limits.
- `test_acceptance_real_keyword_recall_reads_the_relational_store` - the unpatched `keyword_recall` returns only matching rows of the requested knowledge base, with the stored row identity and the real keyword scores in descending order.
- `test_acceptance_real_keyword_recall_chunks_long_text_and_caps_results` - the unpatched `keyword_recall` splits long stored text at the real character offsets, drops chunks without a keyword hit, and caps the result at `top_k`.
- `test_acceptance_retrieve_knowledge_uses_the_real_keyword_recall` - the real keyword chunk travels through fusion and rerank into the final context.

### Goal 4 - Rerank and provider smoke (`tests/test_provider_smoke.py`, `scripts/smoke_providers.py`)

Covers the provider clients the chain depends on - embedding, rerank, DeepSeek chat and
the text fallback - for the success contract, failures, timeouts and malformed
responses. Every error path must be visible: a logged error, a failed trace, or an
explicit error event.

- `test_embedding_request_contract_and_vectors` - URL, payload, authorization header, timeout, returned vectors and no warning on success.
- `test_embedding_without_credentials_never_opens_a_connection` - missing credentials use the hash fallback without creating an HTTP client.
- `test_embedding_timeout_raises_instead_of_falling_back` - a timeout raises `EmbeddingBackendError` and logs the failure, instead of falling back to hash vectors.
- `test_embedding_malformed_response_raises_instead_of_falling_back[empty-data|empty-vector|count-mismatch|dimension-mismatch]` - malformed responses raise `EmbeddingBackendError` (missing vectors, extra vectors and wrong dimensions) and are logged.
- `test_rerank_request_contract_clamps_and_sorts_scores` - rerank request contract, score clamping to `[0, 1]` and descending order.
- `test_rerank_timeout_reports_failed_trace` - a rerank timeout produces a failed trace with the provider and error.
- `test_rerank_malformed_response_reports_failed_trace[no-results|index-out-of-range|non-numeric-index]` - malformed rerank responses fail loudly instead of degrading to an empty success.
- `test_rerank_without_api_key_reports_failed_trace` - a missing rerank key fails without sending a request.
- `test_deepseek_model_configuration_contract` - DeepSeek client configuration: base URL normalization, model normalization, `timeout=60`, `max_retries=0`.
- `test_deepseek_model_without_api_key_raises` / `test_text_fallback_model_without_api_key_raises` - missing credentials raise instead of degrading.
- `test_answer_stream_reports_error_when_fallback_disabled` / `test_answer_stream_reports_error_when_fallback_unconfigured` - an unusable fallback emits an explicit error event.
- `test_answer_stream_switches_to_text_fallback_model` - a failing DeepSeek stream switches to the text fallback model.
- `test_smoke_script_runs_offline_and_reports_every_provider` - `scripts/smoke_providers.py` runs offline without credentials, reports every provider and exits 0.
- `test_smoke_script_live_without_credentials_reports_skips_not_passes` - `--live` without credentials reports every provider as skipped and never claims a pass.

Live connectivity for that last goal is intentionally out of the offline test net: run
`python scripts/smoke_providers.py --live` in the target environment for it.

## Closure Decision

This phase is closed as a minimal maintenance-goal closure. The current state is verified by automated tests and build, but the original open-ended maintenance objective should not be treated as globally complete. Future work should use the follow-up goals above instead of continuing this broad goal indefinitely.
