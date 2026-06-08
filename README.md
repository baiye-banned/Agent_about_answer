# 企业知识库 Advanced RAG 智能问答系统

一个基于 Vue 3 + FastAPI 的企业知识库问答平台。项目支持登录鉴权、知识库管理、文件上传解析、多知识库隔离检索、图片附件问答、SSE 流式回答、多轮会话记忆和 RAGAS 在线评估。

## 1. 架构说明

### 整体架构

```text
Vue 3 前端
  -> Pinia / Vue Router / Element Plus
  -> Fetch SSE 调用 /api/chat/stream
  -> FastAPI 路由层
  -> JWT 鉴权与用户上下文解析
  -> 会话记忆 / 图片理解 / RAG 路由判断
  -> retrieve_knowledge 检索链路
  -> DeepSeek / DashScope / Milvus / MySQL
  -> SSE 流式返回答案、来源、Trace 和完成事件
```

### 后端分层

```text
backend/
  router/       API 路由挂载，负责把 HTTP 请求转到 service
  service/      业务编排，例如聊天流、知识库上传、删除回滚
  rag/          RAG 核心能力：LLM、Milvus、rerank、记忆、Trace、RAGAS、视觉理解
  database/     SQLAlchemy Session、MySQL 初始化、checkpointer
  model/        ORM 数据模型
  schema/       Pydantic 请求与响应结构
  crud/         数据访问逻辑
```

### 前端分层

```text
src/
  api/          Axios / Fetch API 封装，包含 streamChat
  stores/       Pinia 状态管理
  views/        Chat、Knowledge、Login、Profile 页面
  components/   Markdown、Trace、通用展示组件
  utils/        SSE 解析、文件校验、日期格式化等工具
```

### 数据与检索架构

- MySQL 保存用户、会话、消息、知识库、文件元数据和评估结果。
- Milvus / Milvus Lite 保存文档 chunk 向量，使用 `knowledge_base_id` 做多知识库隔离。
- 上传文件时先做文件校验、文本抽取和语义分块，再写入 MySQL 与 Milvus；失败时执行回滚。
- 删除知识库或文件时会清理对应 SQL 记录和向量数据，尽量保持元数据与向量索引一致。
- 聊天主链路不再经过 Agent / Tool 包装，当前核心检索入口是 `backend/rag/retrieval.py` 中的 `retrieve_knowledge()`。

### RAG 主链路

```text
用户问题
  -> streamChat()
  -> /api/chat/stream
  -> stream_chat()
  -> 解析 JWT 和当前知识库
  -> 构建 effective_question
  -> 构建短期记忆与长期摘要上下文
  -> decide_need_rag 判断是否需要检索
  -> build_query_plan 生成 HyDE、改写问题、关键词
  -> Milvus 向量召回 + 关键词召回
  -> RRF 融合
  -> rerank 重排
  -> 拼接知识库上下文
  -> stream_rag_answer 流式生成答案
  -> 保存 assistant 消息
  -> 异步 RAGAS 评估
```

## 2. 关键 Prompt 与 Vibe 思路

### Prompt 角色划分

项目中的 Prompt 按职责拆成几类，而不是把所有要求塞进一个大 Prompt：

| Prompt 类型 | 作用 |
| --- | --- |
| RAG 路由 Prompt | 判断当前问题是否需要进入知识库检索 |
| 查询规划 Prompt | 生成 HyDE 文档、问题改写和关键词 |
| 回答生成 Prompt | 约束模型优先基于知识库上下文回答 |
| 直答 Prompt | 当问题不需要检索时，直接结合会话记忆回答 |
| 视觉理解 Prompt | 将图片附件转成可和文本问题融合的描述 |
| RAGAS 评估 Prompt | 对回答相关性、忠实度、上下文精度做在线评估 |

### Vibe 思路

这个项目的 Prompt 设计目标不是追求复杂 Agent 行为，而是让模型在企业知识库场景下更稳定：

- 明确模型身份：企业知识库问答助手。
- 明确证据优先级：知识库上下文优先，其次是当前问题，再其次是会话记忆。
- 明确禁止行为：不要编造知识库中没有出现的事实。
- 明确低置信度策略：信息不足时说明不足，而不是强行回答。
- 明确输出结构：路由和查询规划使用 JSON，方便后端解析和兜底。
- 保持链路可控：LLM 负责理解和生成，检索、融合、重排、保存、评估由确定性代码完成。

### 核心 Prompt 示例

回答生成阶段会将问题、会话记忆和知识库上下文分区传入模型：

```text
你是企业知识库智能问答助手。
请优先基于提供的企业知识库上下文回答用户问题。
如果知识库信息不足，请明确说明不足之处，并给出可验证的建议。
回答使用中文，结构清晰，避免编造未在上下文中出现的事实。
信息优先级为：企业知识库上下文 > 当前用户问题 > 会话记忆。
会话记忆只能用于理解指代、延续任务和用户偏好，不能替代知识库事实依据。
```

查询规划阶段要求模型只输出 JSON：

```text
请为用户问题生成：
1. 一段可能出现在企业文档里的假设答案文档 hyde_document；
2. 3 个语义不同但意图一致的检索改写 rewrites；
3. 不超过 8 个中文关键词 keywords。
只输出 JSON，不要输出 Markdown。
```

## 3. AI 调用逻辑（流式 / function calling 等）

### 当前 AI 调用方式

项目当前主要使用三类 AI 调用：

| 调用类型 | 位置 | 说明 |
| --- | --- | --- |
| SSE 流式生成 | `/api/chat/stream`、`stream_rag_answer()` | 后端逐块读取模型输出，再通过 `text/event-stream` 返回前端 |
| JSON 结构化调用 | `decide_need_rag()`、`build_query_plan()` | 要求模型返回 JSON，用于路由判断和查询规划 |
| Embedding / rerank 调用 | Milvus 检索、DashScope rerank | 将文本转向量并对候选 chunk 重排 |

### 流式回答链路

```text
前端 streamChat()
  -> fetch('/api/chat/stream')
  -> readStreamEvents(response.body)
  -> 后端 StreamingResponse(event_stream)
  -> stream_rag_answer()
  -> ChatOpenAI.astream()
  -> 前端边接收边渲染
```

SSE 事件主要包含：

- 普通文本 chunk：模型生成的回答片段。
- `sources`：最终引用的知识库片段。
- `trace`：本轮 RAG、记忆、评估等调试信息。
- `error`：模型、检索或服务异常。
- `[DONE]`：流式回答结束标记。

### 模型与服务

- DeepSeek OpenAI-compatible API：主要用于回答生成和部分结构化文本调用。
- DashScope embedding：用于文档向量化和问题向量化。
- DashScope rerank：用于对候选 chunk 做相关性重排。
- DashScope / OpenAI-compatible vision：用于图片附件理解。
- RAGAS：用于 assistant 消息保存后的异步质量评估。

## 4. 部署步骤说明（含 DNS / HTTPS 说明）

### 4.1 环境准备

建议环境：

- Node.js 18+
- Python 3.10+
- MySQL 8+
- Milvus Lite 本地文件或远程 Milvus 服务
- 可用的 DeepSeek / DashScope API Key
- 可选：阿里云 OSS，用于图片附件存储

安装前端依赖：

```bash
npm install
```

安装后端依赖：

```bash
cd backend
pip install -r requirements.txt
```

### 4.2 配置环境变量

复制示例配置：

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

至少需要配置：

```env
MYSQL_USER=your_mysql_user
MYSQL_PASSWORD=your_mysql_password
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_DATABASE=rag_system
MYSQL_SSL_MODE=
MYSQL_SSL_CA=

DEEPSEEK_API_KEY=your_deepseek_api_key
DASHSCOPE_API_KEY=your_dashscope_api_key

SECRET_KEY=replace-with-a-long-random-secret
```

Render 后端 Web Service 建议额外设置 Python 版本，避免平台默认使用过新的 Python 版本：

```env
PYTHON_VERSION=3.10.11
```

如果使用 Aiven MySQL 这类要求 SSL 的云数据库，Render 环境变量里加：

```env
MYSQL_SSL_MODE=required
```

如果服务商要求指定 CA 证书，可以把证书文件随部署环境挂载后设置：

```env
MYSQL_SSL_CA=/path/to/ca.pem
```

如果使用本地 Milvus Lite：

```env
MILVUS_LITE_URI=./milvus.db
```

如果使用远程 Milvus：

```env
MILVUS_URI=http://your-milvus-host:19530
MILVUS_TOKEN=
MILVUS_USER=
MILVUS_PASSWORD=
MILVUS_DB_NAME=
```

### 4.3 初始化数据库

创建 MySQL 数据库：

```sql
CREATE DATABASE IF NOT EXISTS rag_system
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
```

启动 FastAPI 时，`backend/main.py` 会调用 `init_db()` 创建表结构，并调用 `seed_default_users()` 初始化默认用户数据。

### 4.4 本地启动

启动后端：

```bash
cd backend
python -m uvicorn main:app --host 127.0.0.1 --port 8002
```

健康检查：

```bash
curl http://127.0.0.1:8002/health
```

启动前端：

```bash
npm run dev
```

Vite 默认地址：

```text
http://localhost:5173
```

### 4.5 生产构建

构建前端：

```bash
npm run build
```

构建产物位于：

```text
dist/
```

后端可使用 Uvicorn / Gunicorn + Uvicorn Worker 运行。例如：

```bash
cd backend
python -m uvicorn main:app --host 127.0.0.1 --port 8002
```

### 4.6 Nginx 反向代理示例

下面示例假设：

- 域名：`example.com`
- 前端静态文件目录：`/var/www/rag/dist`
- 后端地址：`http://127.0.0.1:8002`

```nginx
server {
    listen 80;
    server_name example.com;

    root /var/www/rag/dist;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8002/api/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_buffering off;
        proxy_read_timeout 300s;
    }

    location /uploads/ {
        proxy_pass http://127.0.0.1:8002/uploads/;
    }
}
```

`proxy_buffering off` 对 SSE 很重要，否则流式回答可能被 Nginx 缓冲，导致前端不能实时显示。

### 4.7 DNS 配置

如果要绑定域名，需要在域名服务商处添加解析记录：

| 类型 | 主机记录 | 指向 |
| --- | --- | --- |
| A | `@` | 服务器公网 IPv4 |
| A | `www` | 服务器公网 IPv4 |
| CNAME | `api` | 可选，指向主域名或后端网关域名 |

配置后可用以下命令检查解析：

```bash
nslookup example.com
```

或：

```bash
dig example.com
```

DNS 生效可能需要几分钟到数小时，取决于 TTL 和域名服务商。

### 4.8 HTTPS 配置

生产环境建议使用 HTTPS。可以通过 Let's Encrypt 申请免费证书：

```bash
sudo certbot --nginx -d example.com -d www.example.com
```

证书签发后，确认 Nginx 中存在 443 配置，并将 HTTP 自动跳转到 HTTPS：

```nginx
server {
    listen 80;
    server_name example.com www.example.com;
    return 301 https://$host$request_uri;
}
```

HTTPS 部署后需要检查：

- `https://example.com` 可以打开前端页面。
- `/api/health` 可以正常返回。
- `/api/chat/stream` 流式输出不会被代理缓冲。
- `VITE_API_BASE_URL` 与 Nginx 代理路径一致。
- 生产环境 `SECRET_KEY` 已替换为强随机值。
- CORS、Cookie、安全响应头按真实部署域名收紧。

## 项目亮点

- 支持登录、JWT 鉴权、用户资料、头像上传和路由守卫。
- 支持知识库创建、重命名、删除、切换、默认知识库兜底和文件管理。
- 支持文本、DOCX、PDF 上传解析、语义分块、Milvus Lite 向量索引和上传失败回滚。
- 基于 `knowledge_base_id` 同时隔离 MySQL 元数据和 Milvus 向量记录。
- 提供 `/api/chat/stream` SSE 流式问答，支持历史会话、Markdown 渲染、来源展示和 Trace 回放。
- 使用 Advanced RAG 检索链路，覆盖 RAG 路由判断、查询规划、HyDE、问题改写、关键词召回、向量召回、RRF 融合、rerank 和兜底检索。
- 支持图片附件问答，将视觉模型生成的图片描述与用户文本合并为 `effective_question`。
- 设计多轮会话记忆机制，包含短期滑窗、长期摘要、近期上下文压缩和长期摘要二次压缩。
- 在 assistant 消息保存后异步执行 RAGAS 在线评估，计算 faithfulness、response relevancy 和 context precision。
- 补充后端 RAG 检索链路和前端流式解析工具函数的回归测试。

## 技术栈

| 层级 | 技术 |
| --- | --- |
| 前端 | Vue 3、Vite、Element Plus、Pinia、Vue Router、TailwindCSS、Axios、Fetch streaming |
| 后端 | FastAPI、SQLAlchemy、MySQL、python-multipart、httpx |
| RAG / Retrieval | 查询规划、HyDE、多路召回、RRF 融合、rerank |
| 向量库 | Milvus Lite / Milvus |
| 模型能力 | DeepSeek OpenAI-compatible API、DashScope embedding / rerank / vision |
| 评估 | RAGAS |
| 对象存储 | 阿里云 OSS |

## 目录结构

```text
.
├── src/                         # Vue 前端
│   ├── api/                     # Axios / Fetch API 封装
│   ├── stores/                  # Pinia 状态管理
│   ├── utils/                   # 前端工具函数和流式解析
│   ├── views/                   # Chat、Knowledge、Login、Profile
│   └── components/              # Markdown、Trace 等组件
├── backend/                     # FastAPI 后端
│   ├── crud/                    # SQLAlchemy 数据访问
│   ├── database/                # DB session 和 checkpointer
│   ├── model/                   # SQLAlchemy 模型
│   ├── rag/                     # Milvus、LLM、记忆、Trace、RAGAS、视觉、检索
│   ├── router/                  # API 路由挂载
│   ├── schema/                  # Pydantic schema
│   └── service/                 # 业务服务
├── docs/                        # 架构和流程文档
├── tests/                       # Node 和 pytest 回归测试
├── package.json
├── pytest.ini
└── backend/requirements.txt
```

## 测试与构建

Node 测试：

```bash
npm test
```

Python 测试：

```bash
python -m pytest -q tests
```

RAGAS 是可选评估能力。默认部署依赖不会安装 RAGAS，以减少 Render 构建时间；如果需要启用在线评估，请额外安装：

```bash
cd backend
pip install -r requirements-ragas.txt
```

前端构建：

```bash
npm run build
```

当前 Vite 构建可能出现 `Chat` chunk 体积较大的提示，这是 bundle size 提醒，不代表构建失败。

## 安全说明

- `.env`、本地数据库、上传文件、日志、PID 文件、缓存、`node_modules` 和构建产物都应加入 Git 忽略规则。
- `.env.example` 只保留占位配置，不应提交真实 API key、OSS 凭证、数据库密码或 JWT secret。
- 生产环境需要替换默认本地配置，配置 HTTPS / 反向代理，强化 JWT secret 管理，并进行外部模型与对象存储连通性检查。

## 文档

- `docs/PROJECT_ARCHITECTURE_FULL.md`：完整架构和模块说明。
- `docs/PROJECT_CODE_READING_ROADMAP.md`：代码阅读路线。
- `docs/PROJECT_FLOW_DIAGRAM.md`：聊天和知识库主流程 Mermaid 图。
- `docs/MAINTENANCE_GOAL_CLOSURE.md`：维护收束报告和剩余风险。

## License

当前仓库主要用于个人学习和实习作品集展示。如需作为公开可复用项目，请补充明确的开源许可证。
