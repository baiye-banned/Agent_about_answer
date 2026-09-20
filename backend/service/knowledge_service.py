
import logging

from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from rag.milvus_client import EmbeddingBackendError, add_chunks, delete_file_chunks
from crud import knowledge_base as crud_knowledge_base
from crud import knowledge_file as crud_knowledge_file
from database.session import SessionLocal, get_db
from model.models import KnowledgeFile, User
from schema.schemas import KnowledgeBaseRequest
from service.auth_service import get_current_user
from service.utils_service import (
    KNOWLEDGE_UPLOAD_MAX_BYTES,
    KNOWLEDGE_UPLOAD_TYPE_ERROR_MESSAGE,
    knowledge_upload_too_large_message,
    resolve_knowledge_upload_type,
)


logger = logging.getLogger(__name__)

UPLOAD_READ_CHUNK_BYTES = 1024 * 1024
# multipart 的 content-length 含边界和表单字段等开销，预检留出余量，避免误伤刚好达标的文件。
MULTIPART_OVERHEAD_ALLOWANCE_BYTES = 4096


def upload_body_exceeds_limit(content_length: str | None, max_bytes: int) -> bool:
    """content-length 预检：只在声明长度明显超过上限时才判定超限。"""
    try:
        declared_length = int(content_length)
    except (TypeError, ValueError):
        return False
    return declared_length > max_bytes + MULTIPART_OVERHEAD_ALLOWANCE_BYTES


async def read_upload_within_limit(file: UploadFile, max_bytes: int) -> bytes:
    """分块读取并累计计量，超限立刻中断，避免把超大文件整体读进内存。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(400, knowledge_upload_too_large_message())
        chunks.append(chunk)
    return b"".join(chunks)


def get_default_knowledge_base(db: Session, user_id: int):
    knowledge_base = crud_knowledge_base.get_default_knowledge_base(db, user_id)
    if knowledge_base:
        return knowledge_base
    try:
        return crud_knowledge_base.create_knowledge_base(db, "默认知识库", user_id)
    except IntegrityError:
        # 并发首次访问时可能同时创建默认知识库，唯一约束冲突后回读即可。
        db.rollback()
        knowledge_base = crud_knowledge_base.get_default_knowledge_base(db, user_id)
        if knowledge_base:
            return knowledge_base
        raise


def resolve_knowledge_base(db: Session, knowledge_base_id: int | None, user_id: int):
    knowledge_base = crud_knowledge_base.resolve_knowledge_base(db, knowledge_base_id, user_id)
    if knowledge_base:
        return knowledge_base
    if knowledge_base_id:
        # 传了 id 却查不到（不存在或不属于当前用户）统一按 404 处理，不暴露资源存在性。
        raise HTTPException(404, "知识库不存在")
    return get_default_knowledge_base(db, user_id)


def rebuild_existing_knowledge_index():
    db = SessionLocal()
    try:
        for entry in db.query(KnowledgeFile).filter(KnowledgeFile.knowledge_base_id.isnot(None)).all():
            chunks = crud_knowledge_file.chunk_text(entry.content or "", entry.id)
            _warn_on_low_chunk_coverage(entry, entry.content or "", chunks, scope="Knowledge index rebuild")
            try:
                add_chunks(chunks, entry.id, entry.name, entry.knowledge_base_id)
            except Exception as exc:
                # 单个文件失败不阻塞启动：旧索引保持原样，可修复向量化服务后再次重建。
                logger.warning("Knowledge index rebuild failed: file_id=%s error=%s", entry.id, exc, exc_info=True)
    finally:
        db.close()


def _index_failure_detail(exc: Exception) -> str:
    """向量化失败时把可读原因透出给前端，其余写入异常保持原有提示。"""
    if isinstance(exc, EmbeddingBackendError):
        return f"知识文件上传失败：{exc}"
    return "知识文件上传失败，向量库写入异常。"


def _warn_on_low_chunk_coverage(entry, text: str, chunks: list[dict], *, scope: str) -> None:
    """分块覆盖率异常时留痕：入库文本可能远少于原文，不允许静默成功。"""
    coverage = crud_knowledge_file.chunk_coverage_ratio(text, chunks)
    if coverage >= crud_knowledge_file.KNOWLEDGE_INDEX_MIN_COVERAGE_RATIO:
        return
    logger.warning(
        "%s chunk coverage too low: file_id=%s filename=%s chunks=%s covered_chars=%s source_chars=%s coverage=%.1f%%",
        scope,
        entry.id,
        entry.name,
        len(chunks),
        sum(len(chunk.get("text") or "") for chunk in chunks),
        len(text or ""),
        coverage * 100,
    )


def _delete_file_vectors_or_500(file_id: int, *, scope: str, detail: str) -> None:
    try:
        delete_file_chunks(file_id)
    except Exception as exc:
        logger.warning("%s vector cleanup failed: file_id=%s error=%s", scope, file_id, exc, exc_info=True)
        raise HTTPException(500, detail)


def list_knowledge_bases(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = crud_knowledge_base.list_knowledge_bases(db, user.id)
    return [crud_knowledge_base.serialize_knowledge_base(item) for item in rows]


def create_knowledge_base(body: KnowledgeBaseRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "知识库名称不能为空")
    if crud_knowledge_base.knowledge_base_name_exists(db, name, user.id):
        raise HTTPException(400, "知识库名称已存在")
    entry = crud_knowledge_base.create_knowledge_base(db, name, user.id)
    return crud_knowledge_base.serialize_knowledge_base(entry)


def rename_knowledge_base(kid: int, body: KnowledgeBaseRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    entry = crud_knowledge_base.get_knowledge_base(db, kid, user.id)
    if not entry:
        raise HTTPException(404, "知识库不存在")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "知识库名称不能为空")
    if crud_knowledge_base.knowledge_base_name_exists(db, name, user.id, exclude_id=kid):
        raise HTTPException(400, "知识库名称已存在")
    entry = crud_knowledge_base.rename_knowledge_base(db, kid, name, user.id)
    return crud_knowledge_base.serialize_knowledge_base(entry)


def delete_knowledge_base(kid: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    entry = crud_knowledge_base.get_knowledge_base(db, kid, user.id)
    if not entry:
        raise HTTPException(404, "知识库不存在")
    if crud_knowledge_base.count_knowledge_bases(db, user.id) <= 1:
        raise HTTPException(400, "至少保留一个知识库")

    target = crud_knowledge_base.get_fallback_knowledge_base(db, kid, user.id)
    file_ids = [file_entry.id for file_entry in crud_knowledge_base.list_files_for_knowledge_base(db, kid)]
    for file_id in file_ids:
        _delete_file_vectors_or_500(
            file_id,
            scope="Knowledge base",
            detail="知识库删除失败，向量库清理异常，请稍后重试。",
        )
    crud_knowledge_base.delete_knowledge_base_with_files(db, kid, target.id, user.id)
    return {"message": "ok", "fallback_knowledge_base_id": target.id}


def list_knowledge(knowledge_base_id: int | None = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    knowledge_base = resolve_knowledge_base(db, knowledge_base_id, user.id)
    files = crud_knowledge_file.list_knowledge_files(db, knowledge_base.id, user.id)
    return [crud_knowledge_file.serialize_knowledge_file(item) for item in files]


async def upload_knowledge(request: Request, file: UploadFile = File(...), knowledge_base_id: int | None = Form(None), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    knowledge_base = resolve_knowledge_base(db, knowledge_base_id, user.id)

    # 类型校验在读取之前完成：白名单外的文件既不解码也不落库。
    if resolve_knowledge_upload_type(file.content_type, file.filename) is None:
        raise HTTPException(400, KNOWLEDGE_UPLOAD_TYPE_ERROR_MESSAGE)
    # content-length 预检 + 分块读取时二次核验，双保险拦截超限文件。
    if upload_body_exceeds_limit(request.headers.get("content-length"), KNOWLEDGE_UPLOAD_MAX_BYTES):
        raise HTTPException(400, knowledge_upload_too_large_message())

    content = await read_upload_within_limit(file, KNOWLEDGE_UPLOAD_MAX_BYTES)
    text = crud_knowledge_file.extract_file_text(file.filename or "", content)

    try:
        entry = crud_knowledge_file.create_knowledge_file(
            db,
            knowledge_base_id=knowledge_base.id,
            name=file.filename or "unknown",
            size=len(content),
            content=text,
            user_id=user.id,
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning("Knowledge file metadata save failed: filename=%s error=%s", file.filename, exc, exc_info=True)
        raise HTTPException(500, crud_knowledge_file.knowledge_file_save_error_message(exc))

    chunks = crud_knowledge_file.chunk_text(text, entry.id)
    _warn_on_low_chunk_coverage(entry, text, chunks, scope="Knowledge file upload")
    try:
        add_chunks(chunks, entry.id, entry.name, knowledge_base.id)
    except Exception as exc:
        logger.warning("Knowledge file indexing failed: file_id=%s error=%s", entry.id, exc, exc_info=True)
        try:
            delete_file_chunks(entry.id)
        except Exception as cleanup_exc:
            logger.warning("Failed to clean partially indexed chunks: file_id=%s error=%s", entry.id, cleanup_exc, exc_info=True)
        try:
            crud_knowledge_file.delete_knowledge_file(db, entry.id, user.id)
        except SQLAlchemyError as cleanup_commit_exc:
            db.rollback()
            logger.warning("Failed to rollback partially indexed knowledge file: file_id=%s error=%s", entry.id, cleanup_commit_exc, exc_info=True)
        raise HTTPException(500, _index_failure_detail(exc))

    return crud_knowledge_file.serialize_knowledge_file(entry)


def delete_knowledge(fid: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    entry = crud_knowledge_file.get_knowledge_file(db, fid, user.id)
    if not entry:
        # 文件不存在或不属于当前用户，统一 404，不暴露资源存在性。
        raise HTTPException(404, "文件不存在")
    _delete_file_vectors_or_500(
        fid,
        scope="Knowledge file",
        detail="知识文件删除失败，向量库清理异常，请稍后重试。",
    )
    crud_knowledge_file.delete_knowledge_file(db, fid, user.id)
    return {"message": "ok"}


def get_knowledge_detail(fid: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    entry = crud_knowledge_file.get_knowledge_file(db, fid, user.id)
    if not entry:
        raise HTTPException(404, "文件不存在")
    return crud_knowledge_file.serialize_knowledge_file(entry)


def get_knowledge_content(fid: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    entry = crud_knowledge_file.get_knowledge_file(db, fid, user.id)
    if not entry:
        raise HTTPException(404, "文件不存在")
    return crud_knowledge_file.get_knowledge_content(db, fid, user.id)
