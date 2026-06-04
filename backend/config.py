import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")
load_dotenv(ROOT_DIR / ".env.development")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


# MySQL
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "change-me")
MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_PORT = os.getenv("MYSQL_PORT", "3306")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "rag_system")

DATABASE_URL = f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}?charset=utf8mb4"

# Milvus Lite / Milvus server
REBUILD_KNOWLEDGE_INDEX_ON_STARTUP = _env_bool("REBUILD_KNOWLEDGE_INDEX_ON_STARTUP", False)
MILVUS_LITE_URI = os.getenv("MILVUS_LITE_URI", "./milvus.db")
MILVUS_URI = os.getenv("MILVUS_URI", MILVUS_LITE_URI)
MILVUS_TOKEN = os.getenv("MILVUS_TOKEN", "")
MILVUS_USER = os.getenv("MILVUS_USER", "")
MILVUS_PASSWORD = os.getenv("MILVUS_PASSWORD", "")
MILVUS_DB_NAME = os.getenv("MILVUS_DB_NAME", "")
MILVUS_COLLECTION_NAME = os.getenv("MILVUS_COLLECTION_NAME", "")

# SQLite checkpointer
CHECKPOINTER_DB_PATH = os.getenv("CHECKPOINTER_DB_PATH", "./checkpointer.db")

# DeepSeek OpenAI-compatible chat API
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseekv4flash")

# OpenAI-compatible vision chat API for image QA.
# Defaults target Alibaba Cloud Model Studio and reuse DASHSCOPE_API_KEY.
VISION_BASE_URL = os.getenv(
    "VISION_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
)
VISION_API_KEY = os.getenv("VISION_API_KEY") or os.getenv("DASHSCOPE_API_KEY", "")
VISION_MODEL = os.getenv("VISION_MODEL", "qwen3.6-plus")
VISION_OSS_URL_EXPIRES_SECONDS = _env_int("VISION_OSS_URL_EXPIRES_SECONDS", 3600)

# OpenAI-compatible text fallback for final answers when DeepSeek is unavailable.
TEXT_FALLBACK_ENABLED = _env_bool("TEXT_FALLBACK_ENABLED", True)
TEXT_FALLBACK_BASE_URL = os.getenv(
    "TEXT_FALLBACK_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
)
TEXT_FALLBACK_API_KEY = os.getenv("TEXT_FALLBACK_API_KEY") or os.getenv("DASHSCOPE_API_KEY", "")
TEXT_FALLBACK_MODEL = os.getenv("TEXT_FALLBACK_MODEL", "qwen3.6-plus")

# OpenAI-compatible embedding API for semantic retrieval and RAGAS.
# Defaults target DashScope text-embedding-v4. If EMBEDDING_API_KEY is not
# set, reuse DASHSCOPE_API_KEY from Alibaba Cloud Model Studio.
EMBEDDING_BASE_URL = os.getenv(
    "EMBEDDING_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
)
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY") or os.getenv("DASHSCOPE_API_KEY", "")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-v4")
EMBEDDING_DIM = _env_int("EMBEDDING_DIM", 1024)

# Dedicated reranker for retrieved chunks.
RERANK_PROVIDER = os.getenv("RERANK_PROVIDER", "dashscope")
RERANK_MODEL = os.getenv("RERANK_MODEL", "qwen3-rerank")
RERANK_BASE_URL = os.getenv(
    "RERANK_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-api/v1/reranks",
)
RERANK_API_KEY = os.getenv("RERANK_API_KEY") or os.getenv("DASHSCOPE_API_KEY", "")
RERANK_TIMEOUT_SECONDS = _env_int("RERANK_TIMEOUT_SECONDS", 30)
RERANK_LLM_FALLBACK_ENABLED = _env_bool("RERANK_LLM_FALLBACK_ENABLED", True)

# Online RAGAS evaluation.
RAGAS_ENABLED = _env_bool("RAGAS_ENABLED", True)
RAGAS_LLM_MODEL = os.getenv("RAGAS_LLM_MODEL", DEEPSEEK_MODEL)
RAGAS_TIMEOUT_SECONDS = _env_int("RAGAS_TIMEOUT_SECONDS", 180)
RAGAS_METRIC_TIMEOUT_SECONDS = _env_int("RAGAS_METRIC_TIMEOUT_SECONDS", 60)
RAGAS_MAX_CONTEXTS = _env_int("RAGAS_MAX_CONTEXTS", 3)
RAGAS_MAX_CONTEXT_CHARS = _env_int("RAGAS_MAX_CONTEXT_CHARS", 1500)
RAGAS_MAX_ANSWER_CHARS = _env_int("RAGAS_MAX_ANSWER_CHARS", 2000)

# Multi-route retrieval.
RETRIEVAL_ROUTE_TOP_K = _env_int("RETRIEVAL_ROUTE_TOP_K", 8)
RETRIEVAL_RERANK_TOP_N = _env_int("RETRIEVAL_RERANK_TOP_N", 5)

# Agentic retrieval planning.
# controlled: structured query planning before retrieval, default for stability.
# langchain: legacy open-ended LangChain agent planning, useful for experiments.
# fallback: skip model planning and use the memory-aware retrieval question.
AGENT_PLANNER_MODE = os.getenv("AGENT_PLANNER_MODE", "controlled").strip().lower()

# Conversation memory.
MEMORY_WINDOW_TURNS = _env_int("MEMORY_WINDOW_TURNS", 4)
MEMORY_SUMMARY_MAX_CHARS = _env_int("MEMORY_SUMMARY_MAX_CHARS", 15000)
MEMORY_RECENT_MAX_CHARS = _env_int("MEMORY_RECENT_MAX_CHARS", 8000)

# Learning/debug trace.
LEARNING_TRACE_ENABLED = _env_bool("LEARNING_TRACE_ENABLED", True)
LEARNING_TRACE_MAX_TEXT_CHARS = _env_int("LEARNING_TRACE_MAX_TEXT_CHARS", 1200)

# Aliyun OSS for chat image attachments
OSS_ACCESS_KEY_ID = os.getenv("oss_access_key_id") or os.getenv("OSS_ACCESS_KEY_ID", "")
OSS_ACCESS_KEY_SECRET = os.getenv("oss_access_key_secret") or os.getenv("OSS_ACCESS_KEY_SECRET", "")
OSS_BUCKET = os.getenv("oss_bucket") or os.getenv("OSS_BUCKET", "")
OSS_ENDPOINT = os.getenv("oss_endpoint") or os.getenv("OSS_ENDPOINT", "")

# JWT
SECRET_KEY = os.getenv("SECRET_KEY", "change-this-secret-key-in-production")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = _env_int("ACCESS_TOKEN_EXPIRE_MINUTES", 60 * 24)
