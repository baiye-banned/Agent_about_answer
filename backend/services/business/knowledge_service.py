
import logging

from fastapi import Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agentic_rag import agentic_retrieve_knowledge as _agentic_retrieve_knowledge
from chroma_client import add_chunks, delete_file_chunks
from crud import knowledge_base as crud_knowledge_base
from crud import knowledge_file as crud_knowledge_file
from database import SessionLocal, get_db
from models import KnowledgeFile, User
from schemas import KnowledgeBaseRequest
from services.base.auth_service import get_current_user


logger = logging.getLogger(__name__)


async def agentic_retrieve_knowledge(*args, **kwargs):
    return await _agentic_retrieve_knowledge(*args, **kwargs)


def get_default_knowledge_base(db: Session):
    knowledge_base = crud_knowledge_base.get_default_knowledge_base(db)
    if knowledge_base:
        return knowledge_base
    return crud_knowledge_base.create_knowledge_base(db, "?????")


def resolve_knowledge_base(db: Session, knowledge_base_id: int | None):
    knowledge_base = crud_knowledge_base.resolve_knowledge_base(db, knowledge_base_id)
    if knowledge_base:
        return knowledge_base
    if knowledge_base_id:
        raise HTTPException(404, "??????")
    return get_default_knowledge_base(db)


def rebuild_existing_knowledge_index():
    db = SessionLocal()
    try:
        for entry in db.query(KnowledgeFile).filter(KnowledgeFile.knowledge_base_id.isnot(None)).all():
            delete_file_chunks(entry.id)
            chunks = crud_knowledge_file.chunk_text(entry.content or "", entry.id)
            add_chunks(chunks, entry.id, entry.name, entry.knowledge_base_id)
    finally:
        db.close()


def list_knowledge_bases(_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = crud_knowledge_base.list_knowledge_bases(db)
    return [crud_knowledge_base.serialize_knowledge_base(item) for item in rows]


def create_knowledge_base(body: KnowledgeBaseRequest, _user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "?????????")
    if crud_knowledge_base.knowledge_base_name_exists(db, name):
        raise HTTPException(400, "????????")
    entry = crud_knowledge_base.create_knowledge_base(db, name)
    return crud_knowledge_base.serialize_knowledge_base(entry)


def rename_knowledge_base(kid: int, body: KnowledgeBaseRequest, _user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    entry = crud_knowledge_base.get_knowledge_base(db, kid)
    if not entry:
        raise HTTPException(404, "??????")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "?????????")
    if crud_knowledge_base.knowledge_base_name_exists(db, name, exclude_id=kid):
        raise HTTPException(400, "????????")
    entry = crud_knowledge_base.rename_knowledge_base(db, kid, name)
    return crud_knowledge_base.serialize_knowledge_base(entry)


def delete_knowledge_base(kid: int, _user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    entry = crud_knowledge_base.get_knowledge_base(db, kid)
    if not entry:
        raise HTTPException(404, "??????")
    if crud_knowledge_base.count_knowledge_bases(db) <= 1:
        raise HTTPException(400, "?????????")

    target = crud_knowledge_base.get_fallback_knowledge_base(db, kid)
    for file_entry in crud_knowledge_base.list_files_for_knowledge_base(db, kid):
        delete_file_chunks(file_entry.id)
    crud_knowledge_base.delete_knowledge_base_with_files(db, kid, target.id)
    return {"message": "ok", "fallback_knowledge_base_id": target.id}


def list_knowledge(knowledge_base_id: int | None = None, _user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    knowledge_base = resolve_knowledge_base(db, knowledge_base_id)
    files = crud_knowledge_file.list_knowledge_files(db, knowledge_base.id)
    return [crud_knowledge_file.serialize_knowledge_file(item) for item in files]


async def upload_knowledge(file: UploadFile = File(...), knowledge_base_id: int | None = Form(None), _user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    knowledge_base = resolve_knowledge_base(db, knowledge_base_id)
    content = await file.read()
    text = crud_knowledge_file.extract_file_text(file.filename or "", content)

    try:
        entry = crud_knowledge_file.create_knowledge_file(
            db,
            knowledge_base_id=knowledge_base.id,
            name=file.filename or "unknown",
            size=len(content),
            content=text,
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning("Knowledge file metadata save failed: filename=%s error=%s", file.filename, exc, exc_info=True)
        raise HTTPException(500, crud_knowledge_file.knowledge_file_save_error_message(exc))

    chunks = crud_knowledge_file.chunk_text(text, entry.id)
    try:
        add_chunks(chunks, entry.id, entry.name, knowledge_base.id)
    except Exception as exc:
        logger.warning("Knowledge file indexing failed: file_id=%s error=%s", entry.id, exc, exc_info=True)
        try:
            delete_file_chunks(entry.id)
        except Exception as cleanup_exc:
            logger.warning("Failed to clean partially indexed chunks: file_id=%s error=%s", entry.id, cleanup_exc, exc_info=True)
        try:
            crud_knowledge_file.delete_knowledge_file(db, entry.id)
        except SQLAlchemyError as cleanup_commit_exc:
            db.rollback()
            logger.warning("Failed to rollback partially indexed knowledge file: file_id=%s error=%s", entry.id, cleanup_commit_exc, exc_info=True)
        raise HTTPException(500, "????????????????????")

    return crud_knowledge_file.serialize_knowledge_file(entry)


def delete_knowledge(fid: int, _user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    entry = crud_knowledge_file.get_knowledge_file(db, fid)
    if not entry:
        raise HTTPException(404, "?????")
    try:
        delete_file_chunks(fid)
    except Exception as exc:
        logger.warning("Knowledge file vector cleanup failed: file_id=%s error=%s", fid, exc, exc_info=True)
        raise HTTPException(500, "???????????????????????")
    crud_knowledge_file.delete_knowledge_file(db, fid)
    return {"message": "ok"}


def get_knowledge_detail(fid: int, _user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    entry = crud_knowledge_file.get_knowledge_file(db, fid)
    if not entry:
        raise HTTPException(404, "?????")
    return crud_knowledge_file.serialize_knowledge_file(entry)


def get_knowledge_content(fid: int, _user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    entry = crud_knowledge_file.get_knowledge_file(db, fid)
    if not entry:
        raise HTTPException(404, "?????")
    return crud_knowledge_file.get_knowledge_content(db, fid)
