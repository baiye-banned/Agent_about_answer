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
                conn.execute(text("ALTER TABLE messages ADD COLUMN retrieval_trace TEXT"))

    if "conversations" in table_names:
        columns = {column["name"] for column in inspector.get_columns("conversations")}
        if "knowledge_base_id" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE conversations ADD COLUMN knowledge_base_id INTEGER"))

    if "knowledge_files" in table_names:
        columns = {column["name"] for column in inspector.get_columns("knowledge_files")}
        if "knowledge_base_id" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE knowledge_files ADD COLUMN knowledge_base_id INTEGER"))


def _ensure_default_knowledge_base():
    if "knowledge_bases" not in inspect(engine).get_table_names():
        return
    from models import KnowledgeBase

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
