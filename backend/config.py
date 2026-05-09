import os

# MySQL
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "wang111111")
MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_PORT = os.getenv("MYSQL_PORT", "3306")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "rag_system")

DATABASE_URL = f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}?charset=utf8mb4"

# Chroma
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "./chroma_data")
REBUILD_KNOWLEDGE_INDEX_ON_STARTUP = os.getenv(
    "REBUILD_KNOWLEDGE_INDEX_ON_STARTUP",
    "false",
).lower() in {"1", "true", "yes", "on"}

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
VISION_OSS_URL_EXPIRES_SECONDS = int(os.getenv("VISION_OSS_URL_EXPIRES_SECONDS", "3600"))

# OpenAI-compatible text fallback for final answers when DeepSeek is unavailable.
TEXT_FALLBACK_ENABLED = os.getenv("TEXT_FALLBACK_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
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
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1024"))

# Online RAGAS evaluation.
RAGAS_ENABLED = os.getenv("RAGAS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
RAGAS_LLM_MODEL = os.getenv("RAGAS_LLM_MODEL", DEEPSEEK_MODEL)
RAGAS_TIMEOUT_SECONDS = int(os.getenv("RAGAS_TIMEOUT_SECONDS", "90"))
RAGAS_METRIC_TIMEOUT_SECONDS = int(os.getenv("RAGAS_METRIC_TIMEOUT_SECONDS", "30"))
RAGAS_MAX_CONTEXTS = int(os.getenv("RAGAS_MAX_CONTEXTS", "3"))
RAGAS_MAX_CONTEXT_CHARS = int(os.getenv("RAGAS_MAX_CONTEXT_CHARS", "1500"))
RAGAS_MAX_ANSWER_CHARS = int(os.getenv("RAGAS_MAX_ANSWER_CHARS", "2000"))

# Multi-route retrieval.
RETRIEVAL_ROUTE_TOP_K = int(os.getenv("RETRIEVAL_ROUTE_TOP_K", "8"))
RETRIEVAL_RERANK_TOP_N = int(os.getenv("RETRIEVAL_RERANK_TOP_N", "5"))

# Aliyun OSS for chat image attachments
OSS_ACCESS_KEY_ID = os.getenv("oss_access_key_id") or os.getenv("OSS_ACCESS_KEY_ID", "")
OSS_ACCESS_KEY_SECRET = os.getenv("oss_access_key_secret") or os.getenv("OSS_ACCESS_KEY_SECRET", "")
OSS_BUCKET = os.getenv("oss_bucket") or os.getenv("OSS_BUCKET", "")
OSS_ENDPOINT = os.getenv("oss_endpoint") or os.getenv("OSS_ENDPOINT", "")

# JWT
SECRET_KEY = "rag-mock-secret-key-change-in-production"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24
