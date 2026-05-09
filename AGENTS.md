---
name: Enterprise Knowledge Base RAG Agent System
domain: Enterprise Knowledge Base / RAG / Full-stack QA Workspace
tech_stack: Vue3 + Vite + Element Plus + Pinia + Vue Router + TailwindCSS + Axios + FastAPI + SQLAlchemy + MySQL + ChromaDB + DeepSeek + DashScope text-embedding-v4 + RAGAS + Aliyun OSS
---

# AGENTS.md

# 五智能体自治开发范式(最高优先级,整个项目的开发架构核心)（5Agent Harness）

> 通用版・适用于任何全流程自动化项目开发

## 一、架构概述

本范式定义了一套**五智能体协同的全自动开发架构**，通过 `Initializer → Planner → Generator → Tester → Evaluator` 的闭环自治流程，无需人工干预即可完成从项目初始化到全功能交付的开发任务。

------

## 二、全局状态文件（必须自动维护）

### 1. harness.json（调度中枢）

json









```
{
  "stage": "initializer",
  "finished": false,
  "currentFeatureId": null
}
```

**合法阶段**：`initializer` / `planner` / `generator` / `tester` / `evaluator`

⚠️ 任何时刻只允许存在一个阶段。

------

### 2. feature_list.json（任务队列）

json









```
{
  "features": [
    {
      "id": "",
      "title": "",
      "filePath": "",
      "description": "",
      "status": "pending",
      "failureReason": ""
    }
  ]
}
```

**status 允许值**：`pending` / `developed` / `tested` / `done` / `failed`

------

## 三、五大智能体定义（内部自动切换）

### 【智能体 1：Initializer 初始化器】（仅执行一次）

**身份**：项目脚手架专家

**职责**：

1. 根据项目技术栈创建项目脚手架
2. 安装项目所需依赖
3. 创建项目标准目录结构
4. 生成项目核心配置文件
5. 初始化全局状态文件（`harness.json`、`feature_list.json`）

**完成行为**：

- `stage` → `planner`
- 输出：`[INITIALIZER DONE]`

------

### 【智能体 2：Planner 规划器】

**身份**：系统架构与需求拆解专家

**职责**：

1. 读取项目需求文档（如 `claude.md`）
2. 将项目需求拆解为**最小可交付 Feature**
3. 确定 Feature 的开发顺序（基于依赖关系与业务逻辑）
4. 写入 `feature_list.json`

**完成行为**：

- `stage` → `generator`
- 输出：`[PLANNER DONE]`

------

### 【智能体 3：Generator 生成器】

**身份**：高级全栈工程师

**开发规则**：

1. 只读取第一个 `status=pending` 的任务
2. 设置 `harness.currentFeatureId`
3. 严格遵循项目指定的技术栈与规范
4. 严格单任务开发，不跨 Feature 修改
5. 代码注释完整，命名规范
6. UI 风格遵循项目需求（如简约商务风、科技风等）

**完成行为**：

- 将任务 `status` → `developed`
- `stage` → `tester`
- 输出：`[GENERATOR DONE]`

------

### 【智能体 4：Tester 功能测试智能体】

**身份**：自动化测试工程师

⚠️ 系统中**唯一允许质疑代码**的 Agent

**职责**：

1. 针对 

   ```
   currentFeatureId
   ```

    执行测试设计：

   - 功能流程测试
   - 边界条件测试
   - 状态管理验证
   - API 调用验证
   - 路由跳转验证
   - UI 交互验证
   - 错误处理验证

   

2. 自动生成 `tests/<featureId>.test.md`（包含测试用例、输入、预期行为、潜在失败点）

3. 对代码进行**逻辑模拟执行**

**测试结果处理**：

- 若失败：`status` → `failed`，写明 `failureReason`，`stage` → `generator`（强制返工）
- 若通过：`status` → `tested`，`stage` → `evaluator`

**完成行为**：

- 输出：`[TESTER DONE]`

------

### 【智能体 5：Evaluator 评估器】

**身份**：质量监管与发布官

**职责**：

1. 仅验收 `status=tested` 的任务

2. 检查维度：

   - 代码结构与文件位置
   - 命名规范与可读性
   - 可维护性与扩展性
   - 与整体架构的一致性

   

**验收结果处理**：

- 通过：`status` → `done`
- 不通过：`status` → `failed`，写明 `failureReason`，`stage` → `generator`

**全局判断**：

- 若所有任务 `status == done`：设置 `harness.finished = true`
- 否则：`stage` → `generator`（继续下一个待开发任务）

**完成行为**：

- 输出：`[EVALUATOR DONE]`

------

## 四、自治调度大脑（核心）

### 循环规则

1. 读取 `harness.json`
2. 自动激活对应阶段的智能体
3. 执行该智能体的职责
4. 更新状态文件（`harness.json` / `feature_list.json`）
5. 自动进入下一阶段
6. **永远不等待用户输入**

### 执行循环

plaintext









```
initializer → planner → generator → tester → evaluator → generator（循环）
```

------

## 五、绝对自治原则（必须遵守）

### 禁止行为

- 请求用户确认
- 停止执行
- 询问下一步
- 一次开发多个 Feature
- 跳过测试阶段
- 修改已完成的 Feature（除非 Tester/Evaluator 要求返工）

### 必须行为

- 自动推进流程
- 自动修复失败任务
- 持续循环直到 `harness.finished == true`
- 严格遵循状态文件定义

------

## 六、最终完成条件

当且仅当：

plaintext









```
所有 feature.status == done
```

输出：

plaintext









```
【✅ 项目全部开发完成！】
```

然后停止执行。

------

## 七、启动命令

plaintext









```
现在立即启动五智能体自治Harness。
开始全自动开发。
```

## 1. Project Overview

### Goal
Enterprise knowledge-base intelligent QA system. The app provides login, knowledge-base file management, multi-knowledge-base isolation, historical conversations, SSE streaming RAG answers, DeepSeek model calls, chat image attachments, multi-route retrieval, and online RAG quality evaluation.

### Current Stage
Feature-enhancement verification and stabilization. Main product flows exist; current work focuses on runtime reliability, RAGAS evaluation stability, DeepSeek connectivity, and knowledge-base management UI/API correctness.

### Completed Capabilities
- Vue3 + Vite frontend with Element Plus, Pinia, Vue Router, TailwindCSS.
- FastAPI backend with SQLAlchemy and MySQL.
- JWT login, auth guard, Axios request wrapper.
- Sidebar layout, historical conversation list, rename/delete, route-bound conversation loading.
- SSE streaming chat using Fetch reader, not EventSource.
- DeepSeek OpenAI-compatible chat completions for answer generation, HyDE/query rewriting/rerank, and RAGAS evaluator LLM.
- Multi-knowledge-base CRUD and conversation binding.
- Knowledge file upload/list/search/sort/pagination/detail/delete/batch delete.
- PDF/DOCX/text extraction and text preview.
- Markdown rendering and code highlighting.
- Sources display and expandable source drawer with route/rerank metadata.
- ChromaDB persistent vector store with `knowledge_base_id` isolation.
- DashScope `text-embedding-v4` integration through OpenAI-compatible embeddings API.
- HyDE document generation, LLM query rewrites, keyword retrieval, multi-route vector retrieval, RRF fusion, and DeepSeek JSON rerank.
- RAGAS online asynchronous evaluation fields and UI panel.
- Aliyun OSS image attachment upload and signed URL generation for chat image input.
- Enter sends, Shift+Enter inserts newline.
- Active generation survives page switching and is pinned to its conversation.
- Backend root `/` and `/health` endpoints exist.

### Not Yet Complete / Future Work
- Full browser E2E regression is not complete.
- DeepSeek HTTPS access is unstable in the current environment; direct probes to `https://api.deepseek.com/v1/models` failed with TLS EOF from Python/httpx and browser also reported terminated connection to DeepSeek docs.
- RAGAS may still fail when DeepSeek structured output is incompatible or network is unavailable; UI must show failed state instead of infinite pending.
- Startup index rebuild was disabled by default; explicit rebuild workflow should be formalized later.
- No Alembic; schema migration remains lightweight startup `ALTER TABLE`.
- No OCR for scanned PDFs or embedded DOCX images.
- Knowledge-base images do not enter vector retrieval.
- Chat image multimodal support depends on DeepSeek runtime support for `image_url`.
- Frontend build has a large Chat chunk warning; not blocking.

## 2. Architecture

### Stack
- Frontend: Vue3 `<script setup>`, Vite, Element Plus, Pinia, Vue Router 4, TailwindCSS, Axios, Fetch streaming.
- Backend: FastAPI, SQLAlchemy, MySQL, ChromaDB, httpx.
- Auth: JWT Bearer token in `Authorization` header.
- LLM: DeepSeek OpenAI-compatible `/v1/chat/completions`.
- Embedding: DashScope `text-embedding-v4`, OpenAI-compatible base URL `https://dashscope.aliyuncs.com/compatible-mode/v1`, default `EMBEDDING_DIM=1024`.
- Vector store: Chroma persistent directory `backend/chroma_data`.
- Evaluation: RAGAS `Faithfulness`, `ResponseRelevancy`, `LLMContextPrecisionWithoutReference`.
- OSS: Aliyun OSS REST signature through `httpx`; do not reintroduce `oss2` unless explicitly required.

### Data Flow
1. User logs in through `/api/auth/login`; frontend stores token in `localStorage`.
2. Axios wrapper injects bearer token into normal HTTP APIs.
3. User selects/creates a knowledge base.
4. Upload posts to `/api/knowledge/upload` with `knowledge_base_id`.
5. Backend extracts text, stores `KnowledgeFile`, chunks content, and writes chunks to Chroma with metadata: `chunk_id`, `file_id`, `file_name`, `knowledge_base_id`.
6. Chat request sends `conversation_id`, `knowledge_base_id`, `question`, and optional `attachments`.
7. Backend binds new conversations to selected knowledge base; existing conversations keep their bound knowledge base.
8. Retrieval uses original query vector route, HyDE vector route, rewrite vector routes, keyword route, RRF fusion, and DeepSeek rerank.
9. Backend streams SSE:
   - `data: {"type":"conversation","conversation":...}`
   - optional `data: {"type":"sources","sources":[...]}`
   - content chunks as `data: {"content":"..."}`
   - typed errors as `data: {"type":"error","message":"..."}`
   - final `data: [DONE]`
10. Frontend store updates only the target streaming conversation.
11. Assistant message is saved only when model call succeeds.
12. After assistant save, backend sets `ragas_status="pending"` and starts background RAGAS evaluation.
13. Frontend refreshes messages and polls pending/running RAGAS states.

### Multi-Agent Harness Context
- Historical harness files exist: `harness.json`, `feature_list.json`.
- Current convention: do not restart initializer/planner or regenerate `feature_list` unless explicitly requested.
- `harness.json` previously indicated evaluator/finished true and F-001 to F-008 done.
- Normal engineering workflow is preferred unless user explicitly asks to operate the harness.

## 3. Repository Structure

### Root
- `AGENTS.md`: authoritative project context for future agents.
- `package.json`, `vite.config.js`, `tailwind.config.js`, `postcss.config.js`, `index.html`: frontend app/build config.
- `.env`, `.env.development`: frontend/runtime env.
- `harness.json`, `feature_list.json`: historical multi-agent harness state; do not reset casually.
- `dist/`: generated Vite output.
- `node_modules/`: frontend dependencies.

### Backend
- `backend/main.py`: FastAPI app, auth, users, chat streaming, knowledge CRUD, OSS endpoints, startup lifecycle.
- `backend/models.py`: SQLAlchemy models.
- `backend/database.py`: engine/session/init, lightweight schema migrations, default KB setup.
- `backend/config.py`: environment config.
- `backend/chroma_client.py`: Chroma client, DashScope/OpenAI-compatible embeddings, hash fallback, chunk indexing/search helpers.
- `backend/retrieval.py`: HyDE, query rewrites, keyword route, RRF, rerank, DeepSeek URL/model normalization.
- `backend/ragas_eval.py`: asynchronous online RAGAS evaluation and status update.
- `backend/checkpointer.py`: legacy checkpointer helpers.
- `backend/requirements.txt`: Python deps.
- `backend/uploads/avatars/`: local avatar files.
- `backend/chroma_data/`: Chroma persistence.
- `backend/checkpointer.db`: local checkpointer DB.

### Frontend
- `src/main.js`: app bootstrap.
- `src/App.vue`: router shell.
- `src/router/index.js`: routes/auth guard.
- `src/styles/global.css`: Tailwind/global UI styles.
- `src/api/request.js`: Axios instance, auth header, error handling.
- `src/api/auth.js`, `src/api/user.js`, `src/api/chat.js`, `src/api/knowledge.js`: API clients.
- `src/stores/user.js`: auth/profile/avatar store.
- `src/stores/chat.js`: conversations, messages, streaming state, RAGAS polling.
- `src/views/Layout.vue`: sidebar/history/user shell.
- `src/views/Login.vue`: login page.
- `src/views/Chat.vue`: chat UI, KB selector, attachments, RAG Evaluation panel, source drawer.
- `src/views/Knowledge.vue`: multi-KB file manager.
- `src/views/UserProfile.vue`: profile/avatar/password.
- `src/components/MarkdownRenderer.vue`: markdown/code rendering.

## 4. Development Conventions

### Frontend Rules
- Use Vue3 `<script setup>` only; do not use Options API.
- Use Element Plus components and icons.
- Use Pinia for shared state.
- Use Axios wrapper for normal APIs.
- Use Fetch reader for streaming chat because POST body is required.
- Keep UI business-focused: restrained, dense enough for admin workflows, no decorative landing page.
- Avoid nested cards and decorative orb/gradient backgrounds.
- Keep text fitting responsive containers; no overlapping UI.

### Backend Rules
- Keep endpoints in `backend/main.py` unless refactor is explicitly requested.
- Keep schema changes compatible with existing DB using startup `ALTER TABLE`; do not introduce Alembic casually.
- Use `httpx` for external HTTP calls.
- Return readable Chinese errors.
- Do not save failed assistant messages.
- Preserve SSE wire shape; typed `conversation`, `sources`, and `error` events are allowed.

### Prompt/RAG Rules
- RAG answer should identify as enterprise knowledge-base QA assistant.
- Prioritize provided knowledge context.
- If context is insufficient, say what is missing.
- Avoid fabricating facts outside context.
- Answer in Chinese with clear structure.

### Agent Workflow
1. Read `AGENTS.md`.
2. Inspect current working tree and relevant files.
3. Use `rg`/PowerShell search before editing.
4. Make minimal scoped changes.
5. Run backend compile checks.
6. Run frontend build if frontend changed.
7. If Vite build fails with `spawn EPERM`, rerun with elevated permission.
8. Report changed areas, verification, and remaining risks.

## 5. Current State (CRITICAL)

### Most Recent Work
- Fixed default backend startup behavior:
  - `backend/config.py` now has `REBUILD_KNOWLEDGE_INDEX_ON_STARTUP`, default false.
  - `backend/main.py` only calls `_rebuild_existing_knowledge_index()` when that env switch is true.
  - Verified foreground startup reaches `Application startup complete` and `Uvicorn running on http://127.0.0.1:8020`.
- Fixed `Knowledge.vue` corruption that broke new knowledge-base creation:
  - Repaired broken template tags and corrupted Chinese strings in header, search, upload empty state, table columns, detail dialog, create/rename/delete/copy flows.
  - `createKnowledgeBase()` now has `try/catch` and displays backend error or “创建失败，请确认后端服务已启动”.
  - New knowledge-base prompt uses `knowledge-base-dialog` with improved width.
- RAGAS pending mitigation is already implemented:
  - `RAGAS_TIMEOUT_SECONDS=60`.
  - `ragas_eval.py` uses `max_tokens=4096`, per-metric error isolation, Chinese errors, and logs.
  - Frontend polling times out local pending after 75 seconds and prefers backend assistant states over local pending.
- DeepSeek error display is improved:
  - Backend emits typed SSE `error` event for network/API failures.
  - Frontend treats typed error as failure, not assistant content.
- Assistant action toolbar on chat cards moved inside card top-right to avoid hover flicker.

### Verification Completed
- Backend compile passed:
  ```powershell
  python -m py_compile backend\main.py backend\config.py backend\ragas_eval.py
  ```
- Frontend production build passed with elevated permissions:
  ```powershell
  node node_modules\vite\bin\vite.js build
  ```
  Warning only: Chat chunk > 500 kB.

### Current Runtime Caveat
- Background start attempts through this Codex desktop session were unreliable due tool/session issues:
  - `cmd start /b` did not leave a process or logs.
  - `Start-Process` escalation was blocked once by external approval service `503`.
  - Foreground startup command works and no longer blocks on index rebuild.
- User or next agent should start backend manually in a terminal:
  ```powershell
  cd D:\code\AIcoding\RAG\backend
  python -m uvicorn main:app --host 127.0.0.1 --port 8020
  ```

### Next Steps
1. Start backend in a normal terminal with the command above.
2. Confirm:
   ```powershell
   Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8020/health
   ```
3. Open `http://localhost:5174/knowledge`.
4. Test create KB:
   - create non-empty unique name -> success and auto-switch.
   - create duplicate -> shows “知识库名称已存在”.
   - backend down -> explicit failure message.
5. Refresh `http://localhost:5174/chat/65353d4c`; RAG Evaluation should show backend true failed/done state, not infinite pending.
6. If DeepSeek remains unreachable, fix runtime network/proxy before expecting chat/RAGAS success.

### Known Issues / Technical Debt
- DeepSeek API network path is currently unstable; TCP can connect but HTTPS fails with TLS EOF in Python/httpx and docs site may terminate in browser.
- RAGAS depends on DeepSeek and can fail fast due network/structured-output issues; this is expected until DeepSeek connectivity is stable.
- Startup index rebuild is disabled by default; a proper manual rebuild command/endpoint should be added later.
- Knowledge page had severe mojibake; many visible strings were repaired, but full UI text sweep may still be useful.
- No automated test suite.
- Frontend build large chunk warning remains.

## 6. Commands / Workflows

### Frontend
Install:
```powershell
npm install
```

Run dev server:
```powershell
npm run dev
```

Build:
```powershell
node node_modules\vite\bin\vite.js build
```

If build fails with `spawn EPERM`, rerun with elevated permission because esbuild child process is sandbox-blocked.

### Backend
Install:
```powershell
pip install -r backend\requirements.txt
```

Compile check:
```powershell
python -m py_compile backend\main.py backend\models.py backend\database.py backend\chroma_client.py backend\retrieval.py backend\ragas_eval.py backend\config.py
```

Run backend:
```powershell
cd D:\code\AIcoding\RAG\backend
python -m uvicorn main:app --host 127.0.0.1 --port 8020
```

Health:
```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8020/health
```

### Environment Variables
DeepSeek:
```text
DEEPSEEK_API_KEY
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseekv4flash
```

Model aliases:
```text
deepseekv4flash -> deepseek-v4-flash
deepseekv4pro -> deepseek-v4-pro
```

DashScope embedding:
```text
DASHSCOPE_API_KEY=sk-xxx
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIM=1024
```

RAGAS:
```text
RAGAS_ENABLED=true
RAGAS_LLM_MODEL defaults to DEEPSEEK_MODEL
RAGAS_TIMEOUT_SECONDS=60
```

Startup index rebuild:
```text
REBUILD_KNOWLEDGE_INDEX_ON_STARTUP=false
```
Only set true when deliberately rebuilding Chroma:
```powershell
$env:REBUILD_KNOWLEDGE_INDEX_ON_STARTUP="true"
```

MySQL:
```text
MYSQL_USER
MYSQL_PASSWORD
MYSQL_HOST
MYSQL_PORT
MYSQL_DATABASE
```

Aliyun OSS:
```text
oss_access_key_id / OSS_ACCESS_KEY_ID
oss_access_key_secret / OSS_ACCESS_KEY_SECRET
oss_bucket / OSS_BUCKET
oss_endpoint / OSS_ENDPOINT
```

## 7. Important Decisions

- Keep Vue3 `<script setup>`; do not switch to Options API.
- Keep Element Plus, Pinia, Vue Router, TailwindCSS.
- Keep FastAPI + SQLAlchemy + MySQL.
- Keep light startup migrations; no Alembic unless explicitly requested.
- Keep chat streaming via Fetch reader.
- Preserve SSE event compatibility.
- Do not save failed LLM responses as assistant messages.
- Multi-KB isolation is relational `knowledge_base_id` plus Chroma metadata filtering.
- Each conversation binds exactly one knowledge base.
- DashScope `text-embedding-v4` is the default semantic embedding; fallback hash embeddings may remain for robustness.
- Default embedding dimension is 1024; changing dimension requires reindex/rebuild.
- HyDE documents are used only for retrieval and not shown as source/context in final answer.
- Retrieval strategy is multi-route + RRF + DeepSeek rerank.
- RAGAS is online async, no offline eval dataset or labeled answer correctness in current scope.
- RAGAS can partially fail per metric; UI must not stay pending indefinitely.
- Startup Chroma rebuild is opt-in, not default.
- OSS implementation uses REST signatures with `httpx`; do not add `oss2` casually.
- Knowledge-base image OCR/multimodal retrieval is out of scope.

## 8. Constraints

### Do Not
- Do not reset `harness.json` or regenerate `feature_list.json` unless explicitly asked.
- Do not reinitialize the project.
- Do not replace the frontend framework or UI library.
- Do not remove auth guard, sidebar, history list, Markdown renderer, source drawer, or avatar upload.
- Do not break SSE wire shape.
- Do not add broad refactors while fixing narrow bugs.
- Do not assume internet sources are allowed for RAG answers.
- Do not commit or run destructive git operations unless explicitly requested.

### Must Preserve
- Existing login route and JWT behavior.
- Existing `/api` prefix and frontend `VITE_API_BASE_URL` behavior.
- Existing knowledge-base CRUD API shape:
  - `GET /api/knowledge-bases`
  - `POST /api/knowledge-bases { name }`
  - `PUT /api/knowledge-bases/{id} { name }`
  - `DELETE /api/knowledge-bases/{id}`
- Existing file upload and deletion behavior.
- Existing chat stream behavior and conversation routing.
- Existing Chinese business UI copy style.
- Backend runtime port convention for current work: `8020`.
- Frontend preview convention: `http://localhost:5174/`.

### Verified Effective Methods
- `python -m py_compile ...` catches backend syntax errors reliably.
- `node node_modules\vite\bin\vite.js build` validates Vue SFC/template syntax; requires elevated permission if sandbox blocks esbuild.
- Foreground backend startup confirms if lifecycle completes:
  ```powershell
  cd backend
  python -m uvicorn main:app --host 127.0.0.1 --port 8020
  ```
- Disable startup index rebuild by leaving `REBUILD_KNOWLEDGE_INDEX_ON_STARTUP` unset/false.
