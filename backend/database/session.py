from sqlalchemy import create_engine
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from config import DATABASE_URL

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables if they don't exist."""
    Base.metadata.create_all(bind=engine)
    _ensure_schema_columns()
    _ensure_default_knowledge_base()


def _ensure_schema_columns():
    _ensure_mysql_utf8mb4()

    inspector = inspect(engine)
    table_names = inspector.get_table_names()
    if "messages" in table_names:
        columns = {column["name"] for column in inspector.get_columns("messages")}
        if "sources" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE messages ADD COLUMN sources TEXT"))
        if "attachments" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE messages ADD COLUMN attachments TEXT"))
        if "ragas_status" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE messages ADD COLUMN ragas_status VARCHAR(20)"))
        if "ragas_scores" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE messages ADD COLUMN ragas_scores TEXT"))
        if "ragas_error" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE messages ADD COLUMN ragas_error TEXT"))
        if "retrieval_trace" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE messages ADD COLUMN retrieval_trace LONGTEXT"))
        _ensure_mysql_text_column("messages", "content", "LONGTEXT", nullable=False)
        _ensure_mysql_varchar_column("messages", "role", 10, nullable=False)
        _ensure_mysql_text_column("messages", "sources", "TEXT")
        _ensure_mysql_text_column("messages", "attachments", "TEXT")
        _ensure_mysql_varchar_column("messages", "ragas_status", 20)
        _ensure_mysql_text_column("messages", "ragas_scores", "TEXT")
        _ensure_mysql_text_column("messages", "ragas_error", "TEXT")
        _ensure_mysql_text_column("messages", "retrieval_trace", "LONGTEXT")

    if "conversations" in table_names:
        columns = {column["name"] for column in inspector.get_columns("conversations")}
        if "knowledge_base_id" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE conversations ADD COLUMN knowledge_base_id INTEGER"))
        if "memory_summary" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE conversations ADD COLUMN memory_summary TEXT"))
        if "memory_summary_upto_message_id" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE conversations ADD COLUMN memory_summary_upto_message_id INTEGER DEFAULT 0"))
        if "memory_updated_at" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE conversations ADD COLUMN memory_updated_at DATETIME"))
        _ensure_mysql_varchar_column("conversations", "id", 36, nullable=False)
        _ensure_mysql_varchar_column("conversations", "title", 200, nullable=False)
        _ensure_mysql_text_column("conversations", "memory_summary", "TEXT")

    if "knowledge_files" in table_names:
        columns = {column["name"] for column in inspector.get_columns("knowledge_files")}
        if "knowledge_base_id" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE knowledge_files ADD COLUMN knowledge_base_id INTEGER"))
        _ensure_mysql_varchar_column("knowledge_files", "name", 255, nullable=False)
        _ensure_mysql_text_column("knowledge_files", "content", "LONGTEXT")

    if "knowledge_bases" in table_names:
        _ensure_mysql_varchar_column("knowledge_bases", "name", 100, nullable=False)

    if "users" in table_names:
        _ensure_mysql_varchar_column("users", "username", 50, nullable=False)
        _ensure_mysql_varchar_column("users", "password_hash", 255, nullable=False)
        _ensure_mysql_varchar_column("users", "avatar", 500)

    if "chat_trace_sessions" in table_names:
        columns = {column["name"] for column in inspector.get_columns("chat_trace_sessions")}
        _ensure_mysql_varchar_column("chat_trace_sessions", "id", 36, nullable=False)
        _ensure_mysql_varchar_column("chat_trace_sessions", "status", 20)
        if "events" in columns:
            _ensure_mysql_text_column("chat_trace_sessions", "events", "LONGTEXT")


def _ensure_mysql_utf8mb4():
    if engine.dialect.name != "mysql":
        return

    database_name = engine.url.database
    if database_name:
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f"ALTER DATABASE `{database_name}` "
                        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                    )
                )
        except Exception:
            # Existing databases may not allow ALTER DATABASE on all hosts; continue with table conversion.
            pass

    inspector = inspect(engine)
    for table_name in (
        "users",
        "knowledge_bases",
        "conversations",
        "messages",
        "knowledge_files",
        "chat_trace_sessions",
    ):
        if table_name not in inspector.get_table_names():
            continue
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f"ALTER TABLE `{table_name}` "
                        "CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                    )
                )
        except Exception:
            # Table conversion can fail on partially migrated schemas; keep startup alive and
            # let the explicit column migration below handle the critical fields.
            pass


def _ensure_mysql_text_column(
    table_name: str,
    column_name: str,
    sql_type: str,
    nullable: bool | None = None,
):
    _ensure_mysql_character_column(table_name, column_name, sql_type, nullable=nullable)


def _ensure_mysql_varchar_column(
    table_name: str,
    column_name: str,
    length: int,
    nullable: bool | None = None,
):
    _ensure_mysql_character_column(table_name, column_name, f"VARCHAR({length})", nullable=nullable)


def _ensure_mysql_character_column(
    table_name: str,
    column_name: str,
    sql_type: str,
    nullable: bool | None = None,
):
    column = _get_mysql_column_info(table_name, column_name)
    if not column:
        return
    normalized_type = str(sql_type).split("(", 1)[0].lower()
    if (
        str(column.get("data_type", "")).lower() == normalized_type
        and str(column.get("character_set_name") or "").lower() == "utf8mb4"
    ):
        return
    effective_nullable = nullable
    if effective_nullable is None:
        effective_nullable = str(column.get("is_nullable") or "").upper() == "YES"
    null_clause = " NULL" if effective_nullable else " NOT NULL"
    with engine.begin() as conn:
        conn.execute(
            text(
                f"ALTER TABLE `{table_name}` "
                f"MODIFY COLUMN `{column_name}` {sql_type} "
                f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci{null_clause}"
            )
        )


def _get_mysql_column_info(table_name: str, column_name: str):
    if engine.dialect.name != "mysql":
        return None
    with engine.connect() as conn:
        result = conn.execute(
            text(
                """
                SELECT DATA_TYPE AS data_type,
                       CHARACTER_SET_NAME AS character_set_name,
                       COLLATION_NAME AS collation_name,
                       IS_NULLABLE AS is_nullable
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = :table_name
                  AND COLUMN_NAME = :column_name
                """
            ),
            {"table_name": table_name, "column_name": column_name},
        )
        return result.mappings().first()


def _ensure_default_knowledge_base():
    if "knowledge_bases" not in inspect(engine).get_table_names():
        return
    from model.models import KnowledgeBase

    db = SessionLocal()
    try:
        default_base = db.query(KnowledgeBase).order_by(KnowledgeBase.id.asc()).first()
        if not default_base:
            default_base = KnowledgeBase(name="默认知识库")
            db.add(default_base)
            db.commit()
            db.refresh(default_base)

        default_id = default_base.id
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE conversations SET knowledge_base_id = :kid WHERE knowledge_base_id IS NULL"),
                {"kid": default_id},
            )
            conn.execute(
                text("UPDATE knowledge_files SET knowledge_base_id = :kid WHERE knowledge_base_id IS NULL"),
                {"kid": default_id},
            )
    finally:
        db.close()
