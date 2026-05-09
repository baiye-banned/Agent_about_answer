# 企业知识库 RAG 智能问答系统

一个面向企业知识库场景的全栈 RAG 问答系统，支持登录、知识库管理、文件上传解析、多知识库隔离、SSE 流式问答、DeepSeek / DashScope 调用、RAGAS 在线评估和 OSS 图片附件上传。

## 核心能力

- 登录鉴权与用户资料管理
- 知识库创建、重命名、删除与切换
- 文件上传、解析、检索、预览与删除
- 多知识库隔离检索与会话绑定
- SSE 流式回答与历史对话管理
- DeepSeek 生成、重写、HyDE、Rerank
- DashScope `text-embedding-v4` 向量化
- RAGAS 在线评估状态展示
- 阿里云 OSS 图片附件上传与签名访问

## 技术栈

- 前端：Vue 3、Vite、Element Plus、Pinia、Vue Router、TailwindCSS、Axios
- 后端：FastAPI、SQLAlchemy、MySQL、ChromaDB、httpx
- 模型：DeepSeek、DashScope Embedding、RAGAS
- 存储：MySQL、ChromaDB、Aliyun OSS

## 目录结构

```text
.
├── src/                  # 前端源码
├── backend/              # FastAPI 后端
├── README.md
├── .gitignore
├── .env.example
├── package.json
└── backend/requirements.txt
```

## 快速启动

### 1. 前端

```bash
npm install
npm run dev
```

前端默认地址：`http://localhost:5173/`

### 2. 后端

进入 `backend/` 目录后启动：

```bash
pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8002
```

健康检查：

```bash
http://127.0.0.1:8002/health
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

### Chroma / Checkpointer

- `CHROMA_PERSIST_DIR`
- `REBUILD_KNOWLEDGE_INDEX_ON_STARTUP`
- `CHECKPOINTER_DB_PATH`

### DeepSeek

- `DEEPSEEK_API_KEY`
- `DEEPSEEK_BASE_URL`
- `DEEPSEEK_MODEL`

### 视觉问答 / 图片附件

- `VISION_BASE_URL`
- `VISION_API_KEY`
- `VISION_MODEL`
- `VISION_OSS_URL_EXPIRES_SECONDS`

### 文本兜底

- `TEXT_FALLBACK_ENABLED`
- `TEXT_FALLBACK_BASE_URL`
- `TEXT_FALLBACK_API_KEY`
- `TEXT_FALLBACK_MODEL`

### 向量化

- `EMBEDDING_BASE_URL`
- `EMBEDDING_API_KEY`
- `EMBEDDING_MODEL`
- `EMBEDDING_DIM`

### RAGAS

- `RAGAS_ENABLED`
- `RAGAS_LLM_MODEL`
- `RAGAS_TIMEOUT_SECONDS`
- `RAGAS_METRIC_TIMEOUT_SECONDS`
- `RAGAS_MAX_CONTEXTS`
- `RAGAS_MAX_CONTEXT_CHARS`
- `RAGAS_MAX_ANSWER_CHARS`

### OSS

- `OSS_ACCESS_KEY_ID`
- `OSS_ACCESS_KEY_SECRET`
- `OSS_BUCKET`
- `OSS_ENDPOINT`

### JWT

- `SECRET_KEY`：建议改造为环境变量读取；当前代码仍使用 `backend/config.py` 中的固定值
- `ALGORITHM`：当前代码常量为 `HS256`
- `ACCESS_TOKEN_EXPIRE_MINUTES`：当前代码常量为 1440 分钟

## 验证命令

### 后端语法检查

```bash
python -m py_compile backend/main.py backend/models.py backend/database.py backend/chroma_client.py backend/retrieval.py backend/ragas_eval.py backend/config.py
```

### 前端构建

```bash
node node_modules/vite/bin/vite.js build
```

### 忽略规则验证

```bash
git check-ignore -v .env .env.development node_modules dist backend/chroma_data backend/uploads backend/checkpointer.db frontend.log backend.log backend.pid
```

## 常见问题

### 1. 为什么 `.env` 不能提交？

`.env` 通常包含数据库密码、OSS 密钥、API Key 等敏感信息，公开仓库必须排除。

### 2. 为什么仓库里不该提交 `node_modules/` 和 `dist/`？

这两个目录分别是本地依赖和构建产物，可以通过 `package-lock.json` 和 `npm run build` 重新生成。

### 3. 为什么要忽略 `backend/chroma_data/` 和 `backend/uploads/`？

这两个目录属于本地运行数据，包含向量库、上传文件和运行时资源，不适合直接推送到 GitHub。

### 4. 公开仓库还有哪些安全注意点？

当前 `backend/config.py` 里仍有 MySQL 默认密码和固定 `SECRET_KEY`。公开发布前，建议尽快改为环境变量注入并使用更强的随机密钥。

### 5. 后端为什么默认是 `8002`？

这是当前仓库里的实际启动端口，与前端开发代理配置保持一致。

## 发布说明

仓库准备推送到：

```text
https://github.com/baiye-banned/Agent_about_answer.git
```

如果是公开仓库，请先确认 `.env`、本地数据库、向量库、日志和上传文件都没有进入提交记录。
