# 企业知识库 RAG 智能问答系统

一个面向企业知识库场景的全栈 RAG 问答系统。项目支持登录鉴权、知识库管理、文件上传解析、多知识库隔离、SSE 流式回答、图片附件分析、LangChain Agent 检索编排、RAGAS 在线评估、学习中心 Trace 回放和会话记忆管理。

## 核心能力

- 登录鉴权、用户资料和头像管理
- 知识库创建、重命名、删除、切换和默认知识库兜底
- 文本、DOCX、PDF 上传解析、分块、Chroma 索引和失败回滚
- 多知识库隔离检索，基于 `knowledge_base_id` 过滤 SQL 与 Chroma 数据
- `/api/chat/stream` SSE 流式问答，支持历史会话、来源抽屉和 Markdown 渲染
- LangChain Agent + Tool 组织 RAG：路由判断、查询规划、关键词召回、向量召回、RRF 融合、rerank、生成
- DeepSeek OpenAI 兼容调用，DashScope `text-embedding-v4` 向量化
- 图片附件上传 OSS 后做视觉分析，再合并进同一条聊天主链路
- RAGAS 在线评估，assistant 保存后异步计算评分
- 学习中心支持 demo 流程和真实 Trace 回放，可查看变量流、检索过程、记忆管理和 RAGAS 状态
- 会话记忆策略：短期滑窗、滑出轮次写入长期记忆、短期超长语义压缩、长期超长二次摘要

## 技术栈

- 前端：Vue 3、Vite、Element Plus、Pinia、Vue Router、TailwindCSS、Axios、Fetch 流式读取
- 后端：FastAPI、SQLAlchemy、MySQL、ChromaDB、LangChain 1.x、httpx
- LLM / Agent：LangChain `ChatOpenAI`、`create_agent`、DeepSeek OpenAI 兼容接口、文本后备模型
- Embedding：DashScope `text-embedding-v4`，默认维度 1024
- 评估：RAGAS
- 对象存储：阿里云 OSS REST 签名

## 模型与用途

| 功能 | 默认模型 | 说明 |
| --- | --- | --- |
| 最终回答生成 | `deepseekv4flash` | `stream_rag_answer()` 使用，DeepSeek 不可用时切到文本后备模型 |
| RAG 路由判断 | `qwen3.6-plus` | 判断当前问题是否需要检索知识库 |
| 检索规划 / HyDE / 改写 / 关键词生成 | `deepseekv4flash` | 由 `build_query_plan()` 生成检索计划 |
| 检索结果重排 | `deepseekv4flash` | 由 `rerank_chunks()` 重新判断候选 chunk 相关性 |
| 图片内容理解 | `qwen3.6-plus` | 先把图片转成文字描述，再并入问题 |
| 会话记忆压缩 | `deepseekv4flash` | 用于短期记忆压缩和长期记忆摘要 |
| RAGAS 在线评估 | `deepseekv4flash` + `text-embedding-v4` | 用于 faithfulness、context precision、response relevancy |
| 知识库向量化 | `text-embedding-v4` | 上传分块和检索 query 都使用同一 embedding 体系 |

## 目录结构

```text
.
├── src/                         # Vue 前端
│   ├── api/                     # Axios / Fetch API 封装
│   ├── stores/                  # Pinia 状态
│   ├── views/                   # Chat / Knowledge / Learn / UserProfile
│   └── components/              # Markdown、Trace 等组件
├── backend/                     # FastAPI 后端
│   ├── router/                  # HTTP 路由挂载
│   ├── service/                 # 业务编排
│   ├── crud/                    # 数据访问
│   ├── model/                   # SQLAlchemy 模型
│   ├── schema/                  # Pydantic schema
│   ├── database/                # DB session、初始化、checkpoint
│   ├── agent/                   # LangChain Agent
│   ├── tool/                    # LangChain tools / RAG 工具
│   └── rag/                     # Chroma、LLM、Chain、记忆、Trace、RAGAS、视觉
├── docs/                        # 架构、流程图、阅读路线
├── package.json
├── vite.config.js
└── backend/requirements.txt
```

## 快速启动

### 前端

```bash
npm install
npm run dev
```

前端默认地址：`http://localhost:5173/`

`vite.config.js` 会把 `/api` 代理到 `http://localhost:8002`。

### 后端

```bash
cd backend
pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8002
```

健康检查：

```bash
Invoke-WebRequest http://127.0.0.1:8002/health
```

也可以直接运行：

```bash
cd backend
python main.py
```

## 环境变量

建议复制 `.env.example` 后按本地环境填写。

### 前端

- `VITE_API_BASE_URL`：后端 API 前缀，默认 `/api`

### MySQL

- `MYSQL_USER`
- `MYSQL_PASSWORD`
- `MYSQL_HOST`
- `MYSQL_PORT`
- `MYSQL_DATABASE`

### DeepSeek / LangChain

- `DEEPSEEK_API_KEY`
- `DEEPSEEK_BASE_URL`
- `DEEPSEEK_MODEL`
- `TEXT_FALLBACK_ENABLED`
- `TEXT_FALLBACK_BASE_URL`
- `TEXT_FALLBACK_API_KEY`
- `TEXT_FALLBACK_MODEL`

### Embedding / Chroma

- `EMBEDDING_BASE_URL`
- `EMBEDDING_API_KEY`
- `EMBEDDING_MODEL`
- `EMBEDDING_DIM`
- `CHROMA_PERSIST_DIR`
- `REBUILD_KNOWLEDGE_INDEX_ON_STARTUP`

### 记忆与 Trace

- `MEMORY_WINDOW_TURNS`：短期记忆滑窗轮数，默认 `4`
- `MEMORY_RECENT_MAX_CHARS`：短期窗口超长阈值，超过后语义压缩为一条近期记忆
- `MEMORY_SUMMARY_MAX_CHARS`：长期记忆超长阈值，超过后做二次摘要
- `LEARNING_TRACE_ENABLED`
- `LEARNING_TRACE_MAX_TEXT_CHARS`

当前记忆规则：

- 最近窗口保留 `MEMORY_WINDOW_TURNS` 轮。
- assistant 保存后检查滑窗，滑出窗口的完整问答轮次会合并进长期记忆。
- 短期窗口文本超过 `MEMORY_RECENT_MAX_CHARS` 时，调用模型压缩为一条近期记忆；模型失败才使用裁剪兜底。
- 长期记忆超过 `MEMORY_SUMMARY_MAX_CHARS` 时，先做二次摘要；摘要失败才裁剪兜底。
- 学习中心记忆链路字段不做 Trace 截断，便于排查真实上下文。

### 视觉问答 / 图片附件

- `VISION_BASE_URL`
- `VISION_API_KEY`
- `VISION_MODEL`
- `VISION_OSS_URL_EXPIRES_SECONDS`

### RAGAS

- `RAGAS_ENABLED`
- `RAGAS_LLM_MODEL`
- `RAGAS_TIMEOUT_SECONDS`
- `RAGAS_METRIC_TIMEOUT_SECONDS`
- `RAGAS_MAX_CONTEXTS`
- `RAGAS_MAX_CONTEXT_CHARS`
- `RAGAS_MAX_ANSWER_CHARS`

当前评估规则：

- assistant 消息保存成功后，如果本轮走了 RAG，系统才会异步启动 RAGAS。
- 评估会先裁剪回答和上下文，再计算 `faithfulness`、`context_precision_without_reference`、`response_relevancy`。
- 评估结果会回写到 `messages.ragas_status`、`messages.ragas_scores`、`messages.ragas_error`。

### OSS

- `OSS_ACCESS_KEY_ID`
- `OSS_ACCESS_KEY_SECRET`
- `OSS_BUCKET`
- `OSS_ENDPOINT`

### JWT

- `SECRET_KEY`
- `ALGORITHM`
- `ACCESS_TOKEN_EXPIRE_MINUTES`

## 主要链路

### 聊天问答

```text
Chat.vue
-> chatStore.sendMessage()
-> streamChat()
-> POST /api/chat/stream
-> backend/service/chat_service.py::stream_chat()
-> decode_token() / resolve_knowledge_base()
-> _build_effective_question() / _build_memory_context()
-> decide_need_rag()
-> agent.agentic_retrieve_knowledge() / tool.retrieve_knowledge()
-> rag.chains.stream_rag_answer()
-> assistant 消息保存后异步 schedule_ragas_evaluation()
-> SSE content / sources / trace / [DONE]
```

### 知识库上传

```text
Knowledge.vue
-> /api/knowledge/upload
-> backend/service/knowledge_service.py
-> extract_file_text()
-> chunk_text()
-> KnowledgeFile 落库
-> Chroma add_chunks()
-> 失败时回滚 SQL 和索引
```

### 学习中心

学习中心由 `src/views/Learn.vue` 和 `src/views/learnFlowSpec.js` 驱动：

- demo 模式播放当前真实链路的流程图。
- trace 模式读取 `/api/chat/traces/{trace_id}` 或消息 Trace。
- 记忆管理页展示 `recent_text`、`memory_context`、`retrieval_question`、`conv.memory_summary` 等真实变量。
- 流程图文档见 `docs/PROJECT_FLOW_DIAGRAM.md`。

## 验证命令

### 后端语法检查

```powershell
python -m py_compile (Get-ChildItem backend -Recurse -File -Filter *.py | ForEach-Object { $_.FullName })
```

### 前端构建

```bash
node node_modules/vite/bin/vite.js build
```

当前构建可能出现 `Chat` chunk 大于 500kB 的 Vite 警告，这是已知构建体积提醒，不代表构建失败。

### 忽略规则检查

```bash
git check-ignore -v .env .env.development node_modules dist backend/chroma_data backend/uploads backend/checkpointer.db frontend.log backend.log backend.pid
```

## 文档

- `docs/PROJECT_ARCHITECTURE_FULL.md`：完整架构与模块说明
- `docs/PROJECT_CODE_READING_ROADMAP.md`：代码阅读路线
- `docs/PROJECT_FLOW_DIAGRAM.md`：聊天和知识库主流程 Mermaid 图
- `AGENTS.md`：项目约束、开发范式和当前状态
