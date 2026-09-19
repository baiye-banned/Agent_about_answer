import logging
import os
import secrets
from pathlib import Path
from urllib.parse import quote_plus

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
MYSQL_SSL_MODE = os.getenv("MYSQL_SSL_MODE", "").strip().lower()
MYSQL_SSL_CA = os.getenv("MYSQL_SSL_CA", "").strip()

DATABASE_URL = (
    f"mysql+pymysql://{quote_plus(MYSQL_USER)}:{quote_plus(MYSQL_PASSWORD)}@"
    f"{MYSQL_HOST}:{MYSQL_PORT}/{quote_plus(MYSQL_DATABASE)}?charset=utf8mb4"
)

MYSQL_CONNECT_ARGS = {}
if MYSQL_SSL_CA:
    MYSQL_CONNECT_ARGS["ssl"] = {"ca": MYSQL_SSL_CA}
elif MYSQL_SSL_MODE in {"required", "require", "true", "1"}:
    MYSQL_CONNECT_ARGS["ssl"] = {}

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

# Bootstrap accounts. When disabled, seed_default_users() creates nothing.
# Account passwords come from SEED_<USERNAME>_PASSWORD and are never hardcoded.
SEED_DEFAULT_USERS = _env_bool("SEED_DEFAULT_USERS", True)

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
RAGAS_ENABLED = _env_bool("RAGAS_ENABLED", False)
RAGAS_LLM_MODEL = os.getenv("RAGAS_LLM_MODEL", DEEPSEEK_MODEL)
RAGAS_TIMEOUT_SECONDS = _env_int("RAGAS_TIMEOUT_SECONDS", 180)
RAGAS_METRIC_TIMEOUT_SECONDS = _env_int("RAGAS_METRIC_TIMEOUT_SECONDS", 60)
RAGAS_MAX_CONTEXTS = _env_int("RAGAS_MAX_CONTEXTS", 3)
RAGAS_MAX_CONTEXT_CHARS = _env_int("RAGAS_MAX_CONTEXT_CHARS", 1500)
RAGAS_MAX_ANSWER_CHARS = _env_int("RAGAS_MAX_ANSWER_CHARS", 2000)

# Multi-route retrieval.
RETRIEVAL_ROUTE_TOP_K = _env_int("RETRIEVAL_ROUTE_TOP_K", 8)
RETRIEVAL_RERANK_TOP_N = _env_int("RETRIEVAL_RERANK_TOP_N", 5)

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
# The literal below is a public placeholder shipped with the repository; anyone can
# read it, so it must never be used as an actual HS256 signing key.
DEFAULT_SECRET_KEY = "change-this-secret-key-in-production"
ALLOW_INSECURE_DEFAULT_SECRET = _env_bool("ALLOW_INSECURE_DEFAULT_SECRET", False)

SECRET_KEY_SOURCE_ENV = "env"
SECRET_KEY_SOURCE_INSECURE_DEV = "insecure-dev"
SECRET_KEY_SOURCE_UNCONFIGURED = "unconfigured"

SECRET_KEY_ERROR = (
    "SECRET_KEY 未配置，或仍是仓库内置的公开占位值，服务拒绝启动。\n"
    "请在环境变量或 .env 中设置一个足够长的随机值，例如：\n"
    "  python -c \"import secrets; print(secrets.token_urlsafe(48))\"\n"
    "仅本地开发可临时设置 ALLOW_INSECURE_DEFAULT_SECRET=true 放行：该开关让进程使用一次性\n"
    "随机密钥启动，每次重启都会使已签发的 token 全部失效，禁止用于任何对外可访问的部署。"
)

_logger = logging.getLogger(__name__)


class SecretKeyError(RuntimeError):
    """Raised at startup when SECRET_KEY is missing or still the public placeholder."""


def resolve_secret_key(secret_key, allow_insecure_default):
    """Resolve the JWT signing key into ``(key, source)``.

    The repository placeholder is never returned as a usable key. When the value is
    missing or still the placeholder, an ephemeral random key is generated instead, so
    the process can never sign or verify with a publicly known secret -- not even when
    it is imported by a script or test that skips the startup check.
    """
    candidate = (secret_key or "").strip()
    if candidate and candidate != DEFAULT_SECRET_KEY:
        return candidate, SECRET_KEY_SOURCE_ENV
    if allow_insecure_default:
        _logger.warning(
            "SECRET_KEY is not configured; ALLOW_INSECURE_DEFAULT_SECRET=true so this "
            "process runs on an ephemeral random key. Never use it for a deployment "
            "reachable by others, and expect every restart to invalidate issued tokens."
        )
        return secrets.token_urlsafe(48), SECRET_KEY_SOURCE_INSECURE_DEV
    _logger.warning(
        "SECRET_KEY is not configured or is still the public repository placeholder. "
        "The process will refuse to start; set SECRET_KEY to a long random value."
    )
    return secrets.token_urlsafe(48), SECRET_KEY_SOURCE_UNCONFIGURED


def secret_key_is_usable() -> bool:
    """True when the process has a non-placeholder key to sign and verify with.

    That is either an explicitly configured SECRET_KEY, or the local-dev escape hatch
    (ephemeral random key). Only the unconfigured case is unusable.
    """
    return SECRET_KEY_SOURCE != SECRET_KEY_SOURCE_UNCONFIGURED


def ensure_secret_key_configured() -> None:
    """Fail fast when the process would run on an unpredictable-but-unusable config.

    Called from the application startup path: a deployment that forgot to set
    SECRET_KEY must not silently serve traffic.
    """
    if not secret_key_is_usable():
        raise SecretKeyError(SECRET_KEY_ERROR)


SECRET_KEY, SECRET_KEY_SOURCE = resolve_secret_key(
    os.getenv("SECRET_KEY"), ALLOW_INSECURE_DEFAULT_SECRET
)
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = _env_int("ACCESS_TOKEN_EXPIRE_MINUTES", 60 * 24)
