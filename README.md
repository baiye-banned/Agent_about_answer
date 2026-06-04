# Enterprise Knowledge Base Agentic RAG

A full-stack enterprise knowledge-base question answering system built with Vue 3 and FastAPI. The project connects authentication, knowledge-base management, document ingestion, Agentic RAG retrieval, streaming answer generation, multimodal image understanding, conversation memory, and online RAGAS evaluation into one end-to-end workflow.

## Highlights

- Login, JWT authentication, user profile, avatar upload, and protected routes.
- Knowledge-base create, rename, delete, switch, default fallback, and file management.
- Text, DOCX, and PDF upload with parsing, semantic chunking, Milvus Lite vector indexing, and rollback on ingestion failure.
- Multi-knowledge-base isolation through `knowledge_base_id` in both MySQL metadata and vector records.
- `/api/chat/stream` SSE response streaming with conversation history, Markdown rendering, retrieved sources, and learning trace output.
- LangChain Agent + Tool retrieval orchestration with RAG routing, query planning, keyword recall, vector recall, RRF fusion, rerank, quality reflection, and retry fallback.
- DeepSeek OpenAI-compatible chat model integration, DashScope `text-embedding-v4` embeddings, and DashScope rerank support.
- Image attachment QA: images are uploaded to Aliyun OSS, analyzed by a vision model, and merged with the user question as `effective_question`.
- Conversation memory with recent sliding-window context, long-term summaries, recent-context compression, and long-summary compression.
- RAGAS online evaluation after assistant message persistence, including faithfulness, response relevancy, and context precision.
- Learning center pages that visualize `agentic_retrieve_knowledge` and `retrieve_knowledge` as clickable flow diagrams.
- Regression tests for backend RAG utilities and frontend stream/helper modules.

## Tech Stack

| Layer | Stack |
| --- | --- |
| Frontend | Vue 3, Vite, Element Plus, Pinia, Vue Router, TailwindCSS, Axios, Fetch streaming |
| Backend | FastAPI, SQLAlchemy, MySQL, Python multipart upload, httpx |
| RAG / Agent | LangChain 1.x, LangChain OpenAI adapter, Agent + Tool orchestration |
| Vector Store | Milvus Lite by default, configurable Milvus server through `MILVUS_URI` |
| Models | DeepSeek OpenAI-compatible chat API, DashScope embeddings / rerank / vision fallback |
| Evaluation | RAGAS |
| Object Storage | Aliyun OSS REST signature implementation |

## Architecture

```text
Vue Chat UI
  -> Pinia chat store
  -> Fetch SSE /api/chat/stream
  -> FastAPI stream_chat
  -> auth + knowledge-base resolution
  -> image understanding + effective_question
  -> memory context builder
  -> RAG gate
  -> agentic_retrieve_knowledge / retrieve_knowledge
  -> stream_rag_answer
  -> save assistant message
  -> async RAGAS evaluation
  -> SSE chunks / sources / trace / [DONE]
```

```text
Knowledge Upload
  -> file validation
  -> text extraction
  -> semantic chunking
  -> MySQL KnowledgeFile metadata
  -> Milvus Lite add_chunks()
  -> rollback SQL/vector writes on failure
```

## Repository Structure

```text
.
├── src/                         # Vue frontend
│   ├── api/                     # Axios / Fetch API wrappers
│   ├── stores/                  # Pinia stores
│   ├── utils/                   # Frontend helpers and stream parsers
│   ├── views/                   # Chat, Knowledge, Learn, Login, Profile
│   └── components/              # Markdown and trace components
├── backend/                     # FastAPI backend
│   ├── agent/                   # Agentic retrieval orchestration
│   ├── crud/                    # SQLAlchemy data access
│   ├── database/                # DB sessions and checkpoint helpers
│   ├── model/                   # SQLAlchemy models
│   ├── rag/                     # Milvus, LLM, memory, trace, RAGAS, vision
│   ├── router/                  # API route mounting
│   ├── schema/                  # Pydantic schemas
│   ├── service/                 # Business services
│   └── tool/                    # Retrieval tools and rerank helpers
├── docs/                        # Architecture and flow documents
├── tests/                       # Node and pytest regression tests
├── package.json
├── pytest.ini
└── backend/requirements.txt
```

## Quick Start

### 1. Frontend

```bash
npm install
npm run dev
```

The Vite dev server defaults to `http://localhost:5173`.

### 2. Backend

```bash
cd backend
pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8002
```

Health check:

```powershell
Invoke-WebRequest http://127.0.0.1:8002/health
```

### 3. Environment

Copy `.env.example` to `.env` and fill in local credentials:

```bash
cp .env.example .env
```

Important variables:

| Variable | Purpose |
| --- | --- |
| `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_DATABASE` | MySQL connection |
| `MILVUS_LITE_URI` | Local Milvus Lite database file, default `./milvus.db` |
| `MILVUS_URI` | Optional remote Milvus server URI |
| `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL` | Final answer generation and planning |
| `DASHSCOPE_API_KEY` | Shared DashScope key for embedding, rerank, vision, and fallback model |
| `EMBEDDING_MODEL`, `EMBEDDING_DIM` | Embedding model and vector dimension |
| `RERANK_PROVIDER`, `RERANK_MODEL` | Retrieved chunk reranking |
| `RAGAS_ENABLED`, `RAGAS_TIMEOUT_SECONDS` | Online answer quality evaluation |
| `OSS_ACCESS_KEY_ID`, `OSS_ACCESS_KEY_SECRET`, `OSS_BUCKET`, `OSS_ENDPOINT` | Image attachment storage |
| `SECRET_KEY`, `ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES` | JWT configuration |

## Core RAG Flow

1. The user sends a question from `Chat.vue`.
2. The frontend calls `streamChat()` and posts to `/api/chat/stream`.
3. The backend decodes the JWT token and resolves the active knowledge base.
4. If an image is attached, the backend uploads it to OSS, calls the vision model, and builds `effective_question`.
5. Recent and long-term memory are loaded and compressed when needed.
6. The RAG gate decides whether knowledge retrieval is required.
7. `agentic_retrieve_knowledge()` plans retrieval queries and calls `retrieve_knowledge()`.
8. `retrieve_knowledge()` performs keyword recall, vector recall, RRF fusion, and rerank.
9. The final answer is streamed back through SSE.
10. After the assistant message is saved, RAGAS evaluation runs asynchronously and writes scores back to the message.

## Learning Center

The learning center is a project-facing explanation page for reading the real retrieval logic:

- `AgenticRetrievePaper.vue` explains `agentic_retrieve_knowledge`: planning, retrieval attempts, reflection, retry, and best-attempt selection.
- `RetrieveKnowledgePaper.vue` explains `retrieve_knowledge`: query planning, route recall, Milvus search, keyword fallback, RRF fusion, and rerank.

These pages are frontend visual explanations only. They do not change backend runtime logic.

## Tests

Node tests:

```bash
npm test
```

Python tests:

```bash
python -m pytest -q
```

Build:

```bash
npm run build
```

The current Vite build may print a large `Chat` chunk warning. It is a bundle-size warning, not a build failure.

## Security Notes

- Real `.env` files, local databases, uploads, logs, PID files, caches, `node_modules`, and build output are ignored by Git.
- `.env.example` contains placeholders only. Do not commit real API keys, OSS credentials, database passwords, or JWT secrets.
- For production use, replace default local settings, configure HTTPS/proxy behavior, harden JWT secret management, and run provider connectivity checks.

## Documentation

- `docs/PROJECT_ARCHITECTURE_FULL.md`: complete architecture and module notes.
- `docs/PROJECT_CODE_READING_ROADMAP.md`: code reading guide.
- `docs/PROJECT_FLOW_DIAGRAM.md`: Mermaid diagrams for chat and knowledge-base flows.
- `docs/MAINTENANCE_GOAL_CLOSURE.md`: maintenance closure report and residual risks.

## License

This repository is currently for personal learning and internship portfolio demonstration. Add an explicit open-source license before using it as a public reusable project.
