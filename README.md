# 企业知识库 Agentic RAG 智能问答系统

一个基于 Vue 3 + FastAPI 的企业知识库智能问答平台。项目将登录鉴权、知识库管理、文件上传解析、Agentic RAG 检索、流式回答、多模态图片理解、会话记忆和 RAGAS 在线评估串成一条完整链路。

## 项目亮点

- 支持登录、JWT 鉴权、用户资料、头像上传和路由守卫。
- 支持知识库创建、重命名、删除、切换、默认知识库兜底和文件管理。
- 支持文本、DOCX、PDF 上传解析、语义分块、Milvus Lite 向量索引和上传失败回滚。
- 基于 `knowledge_base_id` 同时隔离 MySQL 元数据和向量记录，实现多知识库独立检索。
- 提供 `/api/chat/stream` SSE 流式问答，支持历史会话、Markdown 渲染、来源展示和学习 Trace 输出。
- 使用 LangChain Agent + Tool 组织 RAG 编排流程，覆盖 RAG 路由判断、查询规划、关键词召回、向量召回、RRF 融合、rerank、质量反思和重试兜底。
- 接入 DeepSeek OpenAI 兼容模型、DashScope `text-embedding-v4` 向量模型和 DashScope rerank 能力。
- 支持图片附件问答：图片上传到阿里云 OSS 后调用视觉模型分析，并与用户文本合并为 `effective_question`。
- 设计多轮会话记忆机制，包含短期滑窗、长期摘要、近期上下文压缩和长期摘要二次压缩。
- 在 assistant 消息保存后异步执行 RAGAS 在线评估，计算 faithfulness、response relevancy 和 context precision。
- 学习中心提供 `agentic_retrieve_knowledge` 与 `retrieve_knowledge` 两张可点击流程图，用于理解真实检索链路。
- 补充后端 RAG 工具和前端流式解析 / 工具函数的回归测试，提升维护稳定性。

## 技术栈

| 层级 | 技术 |
| --- | --- |
| 前端 | Vue 3、Vite、Element Plus、Pinia、Vue Router、TailwindCSS、Axios、Fetch 流式读取 |
| 后端 | FastAPI、SQLAlchemy、MySQL、python-multipart、httpx |
| RAG / Agent | LangChain 1.x、LangChain OpenAI adapter、Agent + Tool 编排 |
| 向量库 | 默认使用 Milvus Lite，可通过 `MILVUS_URI` 切换到远程 Milvus 服务 |
| 模型能力 | DeepSeek OpenAI 兼容聊天模型、DashScope embedding / rerank / vision fallback |
| 评估 | RAGAS |
| 对象存储 | 阿里云 OSS REST 签名实现 |

## 系统链路

```text
Vue 聊天界面
  -> Pinia chat store
  -> Fetch SSE /api/chat/stream
  -> FastAPI stream_chat
  -> 鉴权 + 知识库解析
  -> 图片理解 + effective_question
  -> 构建记忆上下文
  -> RAG gate 判断
  -> agentic_retrieve_knowledge / retrieve_knowledge
  -> stream_rag_answer
  -> 保存 assistant 消息
  -> 异步 RAGAS 评估
  -> SSE chunks / sources / trace / [DONE]
```

```text
知识库文件上传
  -> 文件校验
  -> 文本提取
  -> 语义分块
  -> MySQL 保存 KnowledgeFile 元数据
  -> Milvus Lite add_chunks()
  -> 失败时回滚 SQL / 向量写入
```

## 目录结构

```text
.
├── src/                         # Vue 前端
│   ├── api/                     # Axios / Fetch API 封装
│   ├── stores/                  # Pinia 状态管理
│   ├── utils/                   # 前端工具函数和流式解析
│   ├── views/                   # Chat、Knowledge、Learn、Login、Profile
│   └── components/              # Markdown、Trace 等组件
├── backend/                     # FastAPI 后端
│   ├── agent/                   # Agentic 检索编排
│   ├── crud/                    # SQLAlchemy 数据访问
│   ├── database/                # DB session 和 checkpoint
│   ├── model/                   # SQLAlchemy 模型
│   ├── rag/                     # Milvus、LLM、记忆、Trace、RAGAS、视觉
│   ├── router/                  # API 路由挂载
│   ├── schema/                  # Pydantic schema
│   ├── service/                 # 业务服务
│   └── tool/                    # 检索工具和 rerank helper
├── docs/                        # 架构和流程文档
├── tests/                       # Node 与 pytest 回归测试
├── package.json
├── pytest.ini
└── backend/requirements.txt
```

## 快速启动

### 1. 启动前端

```bash
npm install
npm run dev
```

Vite 开发服务器默认地址为 `http://localhost:5173`。

### 2. 启动后端

```bash
cd backend
pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8002
```

健康检查：

```powershell
Invoke-WebRequest http://127.0.0.1:8002/health
```

### 3. 配置环境变量

复制 `.env.example` 为 `.env`，再按本地环境填写配置：

```bash
cp .env.example .env
```

主要变量：

| 变量 | 作用 |
| --- | --- |
| `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_DATABASE` | MySQL 连接配置 |
| `MILVUS_LITE_URI` | 本地 Milvus Lite 数据库文件，默认 `./milvus.db` |
| `MILVUS_URI` | 可选的远程 Milvus 服务地址 |
| `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL` | 回答生成和检索规划 |
| `DASHSCOPE_API_KEY` | DashScope embedding、rerank、vision 和后备模型共用 key |
| `EMBEDDING_MODEL`, `EMBEDDING_DIM` | 向量模型和向量维度 |
| `RERANK_PROVIDER`, `RERANK_MODEL` | 检索结果重排 |
| `RAGAS_ENABLED`, `RAGAS_TIMEOUT_SECONDS` | 在线回答质量评估 |
| `OSS_ACCESS_KEY_ID`, `OSS_ACCESS_KEY_SECRET`, `OSS_BUCKET`, `OSS_ENDPOINT` | 图片附件对象存储 |
| `SECRET_KEY`, `ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES` | JWT 配置 |

## RAG 主流程

1. 用户在 `Chat.vue` 输入问题。
2. 前端调用 `streamChat()`，向 `/api/chat/stream` 发起请求。
3. 后端解析 JWT token，并确定当前知识库。
4. 如果带有图片附件，后端上传图片到 OSS，调用视觉模型生成描述，并构建 `effective_question`。
5. 系统读取最近对话和长期摘要，必要时进行上下文压缩。
6. RAG gate 判断当前问题是否需要检索知识库。
7. `agentic_retrieve_knowledge()` 规划检索 query，并调用 `retrieve_knowledge()`。
8. `retrieve_knowledge()` 执行关键词召回、向量召回、RRF 融合和 rerank。
9. 最终答案通过 SSE 流式返回前端。
10. assistant 消息保存后，后台异步运行 RAGAS 评估并回写分数。

## 学习中心

学习中心用于可视化解释真实检索逻辑：

- `AgenticRetrievePaper.vue`：展示 `agentic_retrieve_knowledge` 的规划、检索尝试、质量反思、重试和最佳结果选择。
- `RetrieveKnowledgePaper.vue`：展示 `retrieve_knowledge` 的查询规划、多路召回、Milvus 搜索、关键词兜底、RRF 融合和 rerank。

这些页面只做前端解释展示，不改变后端真实逻辑。

## 测试与构建

Node 测试：

```bash
npm test
```

Python 测试：

```bash
python -m pytest -q
```

前端构建：

```bash
npm run build
```

当前 Vite 构建可能出现 `Chat` chunk 体积较大的警告，这是 bundle size 提醒，不代表构建失败。

## 安全说明

- `.env`、本地数据库、上传文件、日志、PID 文件、缓存、`node_modules` 和构建产物都已加入 Git 忽略规则。
- `.env.example` 只保留占位配置，不应提交真实 API key、OSS 凭证、数据库密码或 JWT secret。
- 如果用于生产环境，需要替换默认本地配置，配置 HTTPS / 反向代理，强化 JWT secret 管理，并进行外部模型与对象存储连通性检查。

## 文档

- `docs/PROJECT_ARCHITECTURE_FULL.md`：完整架构和模块说明。
- `docs/PROJECT_CODE_READING_ROADMAP.md`：代码阅读路线。
- `docs/PROJECT_FLOW_DIAGRAM.md`：聊天和知识库主流程 Mermaid 图。
- `docs/MAINTENANCE_GOAL_CLOSURE.md`：维护收束报告和剩余风险。

## License

当前仓库主要用于个人学习和实习作品集展示。如需作为公开可复用项目，请补充明确的开源许可证。
