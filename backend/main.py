import json
import logging
import mimetypes
import base64
import hashlib
import hmac
import time
from pathlib import Path
from io import BytesIO
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from typing import Optional
from urllib.parse import quote, urlencode
from uuid import uuid4

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
import httpx
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from config import (
    SECRET_KEY,
    ALGORITHM,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    DEEPSEEK_API_KEY,
    DEEPSEEK_MODEL,
    REBUILD_KNOWLEDGE_INDEX_ON_STARTUP,
    OSS_ACCESS_KEY_ID,
    OSS_ACCESS_KEY_SECRET,
    OSS_BUCKET,
    OSS_ENDPOINT,
    VISION_API_KEY,
    VISION_BASE_URL,
    VISION_MODEL,
    VISION_OSS_URL_EXPIRES_SECONDS,
    TEXT_FALLBACK_ENABLED,
    TEXT_FALLBACK_API_KEY,
    TEXT_FALLBACK_BASE_URL,
    TEXT_FALLBACK_MODEL,
)
from database import get_db, init_db, SessionLocal
from models import User, Conversation, Message, KnowledgeFile, KnowledgeBase, _new_id
from retrieval import deepseek_chat_url, normalize_deepseek_model, retrieve_knowledge
from ragas_eval import schedule_ragas_evaluation
from checkpointer import delete_thread_checkpoints, list_threads


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    _seed_default_users()
    if REBUILD_KNOWLEDGE_INDEX_ON_STARTUP:
        _rebuild_existing_knowledge_index()
    yield


app = FastAPI(title="RAG API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
UPLOAD_DIR = Path(__file__).resolve().parent / "uploads"
AVATAR_DIR = UPLOAD_DIR / "avatars"
AVATAR_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")


@app.get("/")
def root():
    return {"status": "ok", "service": "RAG API"}


@app.get("/health")
def health():
    return {"status": "ok"}


def _seed_default_users():
    db = SessionLocal()
    try:
        if not db.query(User).first():
            for username, password in [("admin", "admin123"), ("demo", "demo123")]:
                db.add(User(
                    username=username,
                    password_hash=pwd_context.hash(password),
                ))
            db.commit()
    finally:
        db.close()


def _get_default_knowledge_base(db: Session) -> KnowledgeBase:
    knowledge_base = db.query(KnowledgeBase).order_by(KnowledgeBase.id.asc()).first()
    if knowledge_base:
        return knowledge_base
    knowledge_base = KnowledgeBase(name="默认知识库")
    db.add(knowledge_base)
    db.commit()
    db.refresh(knowledge_base)
    return knowledge_base


def _resolve_knowledge_base(db: Session, knowledge_base_id: Optional[int]) -> KnowledgeBase:
    if knowledge_base_id:
        knowledge_base = db.query(KnowledgeBase).filter_by(id=knowledge_base_id).first()
        if knowledge_base:
            return knowledge_base
        raise HTTPException(404, "知识库不存在")
    return _get_default_knowledge_base(db)


def _rebuild_existing_knowledge_index():
    db = SessionLocal()
    try:
        from chroma_client import add_chunks, delete_file_chunks

        for entry in db.query(KnowledgeFile).filter(KnowledgeFile.knowledge_base_id.isnot(None)).all():
            delete_file_chunks(entry.id)
            chunks = _chunk_text(entry.content or "", entry.id)
            add_chunks(chunks, entry.id, entry.name, entry.knowledge_base_id)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# auth helpers
# ---------------------------------------------------------------------------

def create_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _decode_token(authorization: str) -> str:
    """Extract and validate the token, return username."""
    if not authorization:
        raise HTTPException(401, "Missing authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Invalid authorization header")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub", "")
    except JWTError:
        raise HTTPException(401, "Invalid token")


def get_current_user(authorization: str = Header(""), db: Session = Depends(get_db)) -> User:
    username = _decode_token(authorization)
    user = db.query(User).filter_by(username=username).first()
    if not user:
        raise HTTPException(401, "User not found")
    return user


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    username: str


class PasswordUpdate(BaseModel):
    old_password: str
    new_password: str


class ChatRequest(BaseModel):
    conversation_id: Optional[str] = None
    knowledge_base_id: Optional[int] = None
    question: str
    attachments: list[dict] = Field(default_factory=list)


class KnowledgeBaseRequest(BaseModel):
    name: str


# ---------------------------------------------------------------------------
# auth endpoints
# ---------------------------------------------------------------------------


@app.post("/api/auth/login")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(username=body.username).first()
    if not user or not pwd_context.verify(body.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    token = create_token(body.username)
    return LoginResponse(token=token, username=body.username)


@app.post("/api/auth/logout")
def logout(_user: User = Depends(get_current_user)):
    return {"message": "ok"}


# ---------------------------------------------------------------------------
# user endpoints
# ---------------------------------------------------------------------------


@app.get("/api/user/profile")
def get_profile(user: User = Depends(get_current_user)):
    return {
        "username": user.username,
        "avatar": user.avatar or "",
        "created_at": user.created_at.isoformat() if user.created_at else "",
    }


@app.put("/api/user/password")
def update_password(body: PasswordUpdate, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    if not pwd_context.verify(body.old_password, user.password_hash):
        raise HTTPException(400, "当前密码不正确")
    user.password_hash = pwd_context.hash(body.new_password)
    db.commit()
    return {"message": "密码修改成功"}


@app.post("/api/user/avatar")
async def upload_avatar(file: UploadFile = File(...),
                        user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    allowed_types = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
    }
    content_type = file.content_type or ""
    ext = allowed_types.get(content_type)
    if not ext:
        raise HTTPException(400, "仅支持 png、jpg、jpeg、webp 格式头像")

    content = await file.read()
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(400, "头像文件不能超过 2MB")

    filename = f"user_{user.id}_{int(datetime.now().timestamp())}{ext}"
    target = AVATAR_DIR / filename
    target.write_bytes(content)

    user.avatar = f"/uploads/avatars/{filename}"
    db.commit()
    return {"avatar": user.avatar}


# ---------------------------------------------------------------------------
# chat endpoints
# ---------------------------------------------------------------------------


@app.get("/api/chat/conversations")
def list_conversations(user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    rows = (
        db.query(Conversation)
        .filter_by(user_id=user.id)
        .order_by(Conversation.updated_at.desc())
        .all()
    )
    return [
        {
            "id": c.id,
            "title": c.title,
            "knowledge_base_id": c.knowledge_base_id,
            "knowledge_base_name": c.knowledge_base.name if c.knowledge_base else "",
            "created_at": c.created_at.isoformat() if c.created_at else "",
            "updated_at": c.updated_at.isoformat() if c.updated_at else "",
        }
        for c in rows
    ]


@app.get("/api/chat/conversations/{cid}")
def get_messages(cid: str, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    conv = db.query(Conversation).filter_by(id=cid, user_id=user.id).first()
    if not conv:
        raise HTTPException(404, "对话不存在")
    return [_serialize_message(m) for m in conv.messages]


@app.delete("/api/chat/conversations/{cid}")
def delete_conversation(cid: str, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    conv = db.query(Conversation).filter_by(id=cid, user_id=user.id).first()
    if not conv:
        raise HTTPException(404, "对话不存在")
    db.delete(conv)
    db.commit()
    # clean checkpointer state
    delete_thread_checkpoints(cid)
    return {"message": "ok"}


class RenameRequest(BaseModel):
    title: str


@app.put("/api/chat/conversations/{cid}")
def rename_conversation(cid: str, body: RenameRequest, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    conv = db.query(Conversation).filter_by(id=cid, user_id=user.id).first()
    if not conv:
        raise HTTPException(404, "对话不存在")
    conv.title = body.title
    db.commit()
    return {"message": "ok"}


@app.get("/api/knowledge-bases")
def list_knowledge_bases(_user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    rows = db.query(KnowledgeBase).order_by(KnowledgeBase.created_at.asc()).all()
    return [
        {
            "id": item.id,
            "name": item.name,
            "file_count": len(item.files),
            "created_at": item.created_at.isoformat() if item.created_at else "",
            "updated_at": item.updated_at.isoformat() if item.updated_at else "",
        }
        for item in rows
    ]


@app.post("/api/knowledge-bases")
def create_knowledge_base(body: KnowledgeBaseRequest,
                          _user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "知识库名称不能为空")
    if db.query(KnowledgeBase).filter_by(name=name).first():
        raise HTTPException(400, "知识库名称已存在")
    entry = KnowledgeBase(name=name)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return {"id": entry.id, "name": entry.name}


@app.put("/api/knowledge-bases/{kid}")
def rename_knowledge_base(kid: int, body: KnowledgeBaseRequest,
                          _user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    entry = db.query(KnowledgeBase).filter_by(id=kid).first()
    if not entry:
        raise HTTPException(404, "知识库不存在")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "知识库名称不能为空")
    duplicated = db.query(KnowledgeBase).filter(KnowledgeBase.name == name, KnowledgeBase.id != kid).first()
    if duplicated:
        raise HTTPException(400, "知识库名称已存在")
    entry.name = name
    db.commit()
    return {"id": entry.id, "name": entry.name}


@app.delete("/api/knowledge-bases/{kid}")
def delete_knowledge_base(kid: int, _user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    entry = db.query(KnowledgeBase).filter_by(id=kid).first()
    if not entry:
        raise HTTPException(404, "知识库不存在")
    if db.query(KnowledgeBase).count() <= 1:
        raise HTTPException(400, "至少保留一个知识库")

    target = db.query(KnowledgeBase).filter(KnowledgeBase.id != kid).order_by(KnowledgeBase.id.asc()).first()
    for conversation in db.query(Conversation).filter_by(knowledge_base_id=kid).all():
        conversation.knowledge_base_id = target.id
    for file_entry in db.query(KnowledgeFile).filter_by(knowledge_base_id=kid).all():
        from chroma_client import delete_file_chunks
        delete_file_chunks(file_entry.id)
        db.delete(file_entry)
    db.delete(entry)
    db.commit()
    return {"message": "ok", "fallback_knowledge_base_id": target.id}


@app.post("/api/chat/attachments")
async def upload_chat_attachment(file: UploadFile = File(...),
                                 _user: User = Depends(get_current_user)):
    allowed_types = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
    }
    content_type = file.content_type or mimetypes.guess_type(file.filename or "")[0] or ""
    ext = allowed_types.get(content_type)
    if not ext:
        raise HTTPException(400, "仅支持 png、jpg、jpeg、webp 图片")

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(400, "图片不能超过 5MB")

    object_key = f"rag-chat/{datetime.now().strftime('%Y/%m/%d')}/{uuid4().hex}{ext}"
    try:
        await _put_oss_object(object_key, content, content_type)
    except Exception as exc:
        raise HTTPException(500, f"OSS 上传失败：{exc}")

    return {
        "name": file.filename or f"image{ext}",
        "size": len(content),
        "content_type": content_type,
        "object_key": object_key,
        "url": _public_oss_url(object_key),
    }


@app.post("/api/chat/stream")
async def stream_chat(body: ChatRequest, authorization: str = Header("")):
    username = _decode_token(authorization)
    db = SessionLocal()
    user = db.query(User).filter_by(username=username).first()
    if not user:
        db.close()
        raise HTTPException(401, "User not found")
    try:
        cid = body.conversation_id
        knowledge_base = _resolve_knowledge_base(db, body.knowledge_base_id)
        raw_question = (body.question or "").strip()
        display_question = raw_question or ("请分析这张图片" if body.attachments else "")
        if not display_question:
            raise HTTPException(400, "问题不能为空")

        effective_question, image_analysis = await _build_effective_question(
            raw_question,
            body.attachments,
        )
        if body.attachments and image_analysis.get("status") == "failed" and not raw_question:
            db.close()

            async def failure_stream():
                analysis_data = json.dumps(
                    {
                        "type": "image_analysis",
                        "analysis": image_analysis,
                    },
                    ensure_ascii=False,
                )
                yield f"data: {analysis_data}\n\n"
                error_data = json.dumps(
                    {
                        "type": "error",
                        "message": image_analysis.get("error") or "图片内容识别失败，请检查清晰度后重新上传。",
                    },
                    ensure_ascii=False,
                )
                yield f"data: {error_data}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(failure_stream(), media_type="text/event-stream")
        conv = db.query(Conversation).filter_by(id=cid, user_id=user.id).first() if cid else None
        if not conv:
            # create new conversation
            cid = _new_id()
            title_source = raw_question or effective_question or display_question
            title = title_source[:30] + ("..." if len(title_source) > 30 else "")
            conv = Conversation(
                id=cid,
                user_id=user.id,
                knowledge_base_id=knowledge_base.id,
                title=title,
            )
            db.add(conv)
            db.commit()
        else:
            knowledge_base = conv.knowledge_base or knowledge_base

        # save user message
        db.add(Message(
            conversation_id=cid,
            role="user",
            content=display_question,
            attachments=json.dumps(body.attachments, ensure_ascii=False),
        ))
        db.commit()

        # retrieve relevant knowledge
        knowledge_chunks, retrieval_trace = await retrieve_knowledge(
            effective_question,
            knowledge_base_id=knowledge_base.id,
            db=db,
        )
        if body.attachments:
            retrieval_trace = retrieval_trace or {}
            retrieval_trace["image_analysis_status"] = image_analysis.get("status", "")
            retrieval_trace["image_analysis_error"] = image_analysis.get("error", "")
            retrieval_trace["image_description"] = image_analysis.get("description", "")
            retrieval_trace["effective_question"] = effective_question
        context = ""
        sources = _build_sources(knowledge_chunks)
        retrieved_contexts = [c.get("content", "") for c in knowledge_chunks if c.get("content")]
        if knowledge_chunks:
            context = "\n\n".join(
                f"[鏉ユ簮: {c['file_name']}]\n{c['content']}"
                for c in knowledge_chunks
            )

        async def event_stream():
            full = ""
            failed = False
            conversation_data = json.dumps({
                "type": "conversation",
                "conversation": {
                    "id": cid,
                    "title": conv.title,
                    "knowledge_base_id": knowledge_base.id,
                    "knowledge_base_name": knowledge_base.name,
                },
            }, ensure_ascii=False)
            if body.attachments:
                analysis_data = json.dumps(
                    {
                        "type": "image_analysis",
                        "analysis": image_analysis,
                    },
                    ensure_ascii=False,
                )
                yield f"data: {analysis_data}\n\n"
            yield f"data: {conversation_data}\n\n"
            if sources:
                data = json.dumps({"type": "sources", "sources": sources}, ensure_ascii=False)
                yield f"data: {data}\n\n"
            async for event in _stream_deepseek_response(effective_question, context):
                if isinstance(event, dict):
                    if event.get("type") == "error":
                        failed = True
                        data = json.dumps(
                            {
                                "type": "error",
                                "message": event.get("message") or event.get("content") or "DeepSeek 网络请求失败",
                            },
                            ensure_ascii=False,
                        )
                        yield f"data: {data}\n\n"
                        break
                    chunk = event.get("content", "")
                else:
                    chunk = event
                data = json.dumps({"content": chunk}, ensure_ascii=False)
                yield f"data: {data}\n\n"
                if not failed:
                    full += chunk

            # save assistant message
            if full and not failed:
                assistant_message = Message(
                    conversation_id=cid,
                    role="assistant",
                    content=full,
                    sources=json.dumps(sources, ensure_ascii=False),
                    ragas_status="pending",
                    retrieval_trace=json.dumps(retrieval_trace, ensure_ascii=False),
                )
                db.add(assistant_message)
                db.commit()
                db.refresh(assistant_message)
                schedule_ragas_evaluation(
                    assistant_message.id,
                    effective_question,
                    full,
                    retrieved_contexts,
                )
            db.close()
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")
    except Exception:
        db.close()
        raise



def _build_sources(chunks: list[dict]) -> list[dict]:
    sources = []
    for index, chunk in enumerate(chunks, start=1):
        content = (chunk.get("content") or "").strip()
        if not content:
            continue
        sources.append({
            "index": index,
            "file_id": chunk.get("file_id", 0),
            "file_name": chunk.get("file_name", "Untitled"),
            "chunk_id": chunk.get("chunk_id", ""),
            "route": chunk.get("route", ""),
            "routes": chunk.get("routes", []),
            "rrf_score": chunk.get("rrf_score"),
            "rerank_score": chunk.get("rerank_score"),
            "rerank_reason": chunk.get("rerank_reason", ""),
            "content": content,
            "excerpt": content[:180] + ("..." if len(content) > 180 else ""),
        })
    return sources


def _loads_json(value: str, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _serialize_message(message: Message) -> dict:
    retrieval_trace = _loads_json(message.retrieval_trace, {})
    if not isinstance(retrieval_trace, dict):
        retrieval_trace = {}

    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "sources": _loads_json(message.sources, []),
        "attachments": _loads_json(message.attachments, []),
        "ragas_status": message.ragas_status or "",
        "ragas_scores": _loads_json(message.ragas_scores, {}),
        "ragas_error": message.ragas_error or "",
        "retrieval_trace": retrieval_trace,
        "image_analysis_status": retrieval_trace.get("image_analysis_status", ""),
        "image_analysis_error": retrieval_trace.get("image_analysis_error", ""),
        "image_description": retrieval_trace.get("image_description", ""),
        "created_at": message.created_at.isoformat() if message.created_at else "",
    }

def _ensure_oss_config():
    if not all([OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET, OSS_BUCKET, OSS_ENDPOINT]):
        raise HTTPException(500, "OSS 环境变量未完整配置")


def _oss_host() -> str:
    endpoint = OSS_ENDPOINT.replace("https://", "").replace("http://", "").rstrip("/")
    if endpoint.startswith(f"{OSS_BUCKET}."):
        return endpoint
    return f"{OSS_BUCKET}.{endpoint}"


def _oss_object_path(object_key: str) -> str:
    return "/" + quote(object_key, safe="/")


def _oss_signature(string_to_sign: str) -> str:
    digest = hmac.new(
        OSS_ACCESS_KEY_SECRET.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


async def _put_oss_object(object_key: str, content: bytes, content_type: str):
    _ensure_oss_config()
    host = _oss_host()
    date = formatdate(usegmt=True)
    resource = f"/{OSS_BUCKET}/{object_key}"
    string_to_sign = f"PUT\n\n{content_type}\n{date}\nx-oss-object-acl:public-read\n{resource}"
    signature = _oss_signature(string_to_sign)
    url = f"https://{host}{_oss_object_path(object_key)}"
    headers = {
        "Authorization": f"OSS {OSS_ACCESS_KEY_ID}:{signature}",
        "Content-Type": content_type,
        "Date": date,
        "Host": host,
        "x-oss-object-acl": "public-read",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.put(url, content=content, headers=headers)
    if response.status_code >= 400:
        raise RuntimeError(f"{response.status_code} {response.text[:200]}")


def _sign_oss_url(object_key: str, expires: int = 3600) -> str:
    _ensure_oss_config()
    expires_at = int(time.time()) + expires
    resource = f"/{OSS_BUCKET}/{object_key}"
    string_to_sign = f"GET\n\n\n{expires_at}\n{resource}"
    signature = _oss_signature(string_to_sign)
    query = urlencode({
        "OSSAccessKeyId": OSS_ACCESS_KEY_ID,
        "Expires": str(expires_at),
        "Signature": signature,
    })
    return f"https://{_oss_host()}{_oss_object_path(object_key)}?{query}"


def _public_oss_url(object_key: str) -> str:
    _ensure_oss_config()
    return f"https://{_oss_host()}{_oss_object_path(object_key)}"



def _openai_chat_url(base_url: str) -> str:
    base_url = (base_url or "").rstrip("/")
    if base_url.endswith("/v1"):
        return f"{base_url}/chat/completions"
    return f"{base_url}/v1/chat/completions"


async def _build_effective_question(
    question: str,
    attachments: Optional[list[dict]] = None,
) -> tuple[str, dict]:
    attachments = attachments or []
    question = (question or "").strip()
    if not attachments:
        return question, {}

    image_analysis = await _analyze_image_attachments(attachments, question)
    description = image_analysis.get("description", "").strip()

    if question and description:
        return f"{question}\n\n图片内容：{description}", image_analysis
    if description:
        return description, image_analysis
    return question, image_analysis


async def _analyze_image_attachments(attachments: list[dict], question: str = "") -> dict:
    image_urls = _build_image_urls(attachments)
    if not image_urls:
        return {
            "status": "failed",
            "description": "",
            "error": "图片附件缺少可访问的 OSS object_key，请重新上传后再试。",
        }
    if not VISION_API_KEY:
        return {
            "status": "failed",
            "description": "",
            "error": "图片内容提取失败，请检查 VISION_MODEL/VISION_API_KEY/OSS URL。",
        }

    prompts = _image_analysis_prompts(question)
    last_error = ""
    last_description = ""
    for prompt in prompts:
        try:
            description = await _request_image_description(prompt, image_urls)
        except Exception as exc:
            last_error = str(exc)
            continue

        description = (description or "").strip()
        if not description:
            last_error = "图片内容提取失败：视觉模型未返回图片描述。"
            continue

        status, error = _classify_image_analysis(description)
        if status == "failed":
            last_error = error or "图片内容识别失败，请检查清晰度后重新上传。"
            last_description = description
            continue

        return {
            "status": status,
            "description": description,
            "error": error,
        }

    if last_description:
        status, error = _classify_image_analysis(last_description)
        if status in {"partial", "success"}:
            return {
                "status": status,
                "description": last_description,
                "error": error,
            }

    return {
        "status": "failed",
        "description": "",
        "error": last_error or "图片内容识别失败，请检查清晰度后重新上传。",
    }


def _image_analysis_prompts(question: str = "") -> list[str]:
    question = (question or "").strip()
    if question:
        return [
            (
                "请先逐字识别图片中可见的文字、题目、选项、表格、数字和图表标签，"
                "再用中文概括图片内容，并明确说明哪些内容看清了，哪些内容不够清晰。"
                "不要编造图片中不存在的信息。"
                f"\n用户问题：{question}\n请重点关注与该问题相关的图片信息。"
            ),
            (
                "请再次仔细查看这张图片，优先识别可见文字、题目、选项、数字和表格。"
                "如果局部模糊，也要尽量保留可辨认部分，并说明无法确认的区域。"
                "不要编造图片中不存在的信息。"
                f"\n用户问题：{question}"
            ),
        ]
    return [
        (
            "请尽可能逐字识别这张图片中的所有可见文字、题目、选项、数字、表格和图表标签，"
            "先抄录能看清的内容，再总结图片整体含义。"
            "如果有模糊或看不清的部分，请明确指出，不要编造。"
        ),
        (
            "请重新检查这张图片，专注于文字识别和局部细节。"
            "请输出可辨认的原文、题目、选项、数字和表格内容，并说明哪些地方无法识别。"
        ),
    ]


async def _request_image_description(prompt: str, image_urls: list[str]) -> str:
    user_content = [{"type": "text", "text": prompt}]
    user_content.extend({"type": "image_url", "image_url": {"url": url}} for url in image_urls)
    payload = {
        "model": VISION_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": "你是图片内容理解助手，负责将图片转写为适合检索和问答的中文文本。"},
            {"role": "user", "content": user_content},
        ],
    }
    headers = {
        "Authorization": f"Bearer {VISION_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(_openai_chat_url(VISION_BASE_URL), json=payload, headers=headers)
        if response.status_code >= 400:
            detail = response.text[:300]
            logger.warning("Vision description failed: status=%s detail=%s", response.status_code, detail)
            raise RuntimeError("图片内容提取失败，请检查 VISION_MODEL/VISION_API_KEY/OSS URL。")
        description = response.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        return (description or "").strip()
    except httpx.HTTPError as exc:
        logger.warning("Vision description network request failed: %s", exc, exc_info=True)
        raise RuntimeError("图片内容提取失败，请检查 VISION_MODEL/VISION_API_KEY/OSS URL。") from exc


def _classify_image_analysis(description: str) -> tuple[str, str]:
    text = (description or "").strip()
    if not text:
        return "failed", "图片内容提取失败：视觉模型未返回图片描述。"

    failure_phrases = [
        "看不清",
        "无法识别",
        "无法看清",
        "无法辨认",
        "无法读取",
        "过于模糊",
        "图片模糊",
        "不清晰",
        "无法确定",
        "难以识别",
        "图片质量",
        "无法完全识别",
    ]
    partial_phrases = [
        "部分",
        "大致",
        "可能",
        "疑似",
        "不够清晰",
        "部分可见",
        "仅能看到",
        "只能识别",
    ]
    useful_markers = [
        "可见",
        "看到",
        "能看到",
        "识别到",
        "文字为",
        "内容为",
        "显示",
        "题目",
        "选项",
        "数字",
        "表格",
        "图表",
    ]

    if any(phrase in text for phrase in failure_phrases):
        has_partial_signal = any(phrase in text for phrase in partial_phrases)
        has_useful_content = any(marker in text for marker in useful_markers)
        if has_partial_signal and has_useful_content:
            return "partial", "图片内容仅部分识别，请结合文字问题查看。"
        return "failed", "图片内容未能清晰识别，请检查图片清晰度后重试。"

    if any(phrase in text for phrase in partial_phrases):
        return "partial", "图片内容仅部分识别，请结合文字问题查看。"

    return "success", ""


async def _stream_deepseek_response(question: str, context: str):
    if not DEEPSEEK_API_KEY:
        yield {"type": "error", "message": _model_missing_error(False)}
        return

    system_prompt = (
        "你是企业知识库智能问答助手。请优先基于提供的企业知识库上下文回答用户问题；"
        "如果知识库信息不足，请明确说明不足之处，并给出可验证的建议。"
        "回答使用中文，结构清晰，避免编造未在上下文中出现的事实。"
    )
    user_prompt = (
        f"用户问题：{question}\n\n"
        f"企业知识库上下文：\n{context or '（未检索到相关知识库内容）'}"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    async for event in _stream_openai_chat_chunks(
        deepseek_chat_url(),
        DEEPSEEK_API_KEY,
        normalize_deepseek_model(DEEPSEEK_MODEL),
        messages,
        "DeepSeek",
    ):
        if isinstance(event, dict) and event.get("type") == "error":
            if _should_use_text_fallback(event):
                logger.warning(
                    "DeepSeek failed with retryable error, switching to text fallback: reason=%s status=%s",
                    event.get("reason"),
                    event.get("status_code"),
                )
                async for fallback_event in _stream_text_fallback_response(messages):
                    yield fallback_event
                return
            yield {
                "type": "error",
                "message": _model_error_message(
                    False,
                    event.get("reason") or "api",
                    event.get("status_code"),
                    DEEPSEEK_MODEL,
                ),
            }
            return
        yield event


async def _stream_text_fallback_response(messages: list[dict]):
    if not TEXT_FALLBACK_ENABLED:
        yield {
            "type": "error",
            "message": "DeepSeek 网络请求失败，且文本后备模型未启用，请设置 TEXT_FALLBACK_ENABLED=true。",
        }
        return
    if not TEXT_FALLBACK_API_KEY:
        yield {
            "type": "error",
            "message": "DeepSeek 网络请求失败，且文本后备模型未配置，请配置 TEXT_FALLBACK_API_KEY 或 DASHSCOPE_API_KEY。",
        }
        return

    async for event in _stream_openai_chat_chunks(
        _openai_chat_url(TEXT_FALLBACK_BASE_URL),
        TEXT_FALLBACK_API_KEY,
        TEXT_FALLBACK_MODEL,
        messages,
        "百炼文本后备模型",
    ):
        if isinstance(event, dict) and event.get("type") == "error":
            yield {
                "type": "error",
                "message": _text_fallback_error_message(
                    event.get("reason") or "api",
                    event.get("status_code"),
                ),
            }
            return
        yield event


async def _stream_openai_chat_chunks(
    url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    provider_label: str,
):
    payload = {
        "model": model,
        "stream": True,
        "messages": messages,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code >= 400:
                    text = await response.aread()
                    detail = text.decode("utf-8", errors="replace")[:300]
                    logger.warning(
                        "%s call failed: status=%s detail=%s",
                        provider_label,
                        response.status_code,
                        detail,
                    )
                    yield {
                        "type": "error",
                        "reason": "api",
                        "status_code": response.status_code,
                        "detail": detail,
                    }
                    return

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        parsed = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = parsed.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        yield content
    except httpx.HTTPError as exc:
        logger.warning("%s network request failed: %s", provider_label, exc, exc_info=True)
        yield {
            "type": "error",
            "reason": "network",
            "status_code": None,
            "detail": str(exc),
        }


def _should_use_text_fallback(event: dict) -> bool:
    if event.get("reason") == "network":
        return True
    status_code = event.get("status_code")
    return isinstance(status_code, int) and 500 <= status_code < 600


def _text_fallback_error_message(reason: str, status_code: Optional[int] = None) -> str:
    if reason == "api":
        status = f"（HTTP {status_code}）" if status_code else ""
        return (
            f"DeepSeek 不可用，且百炼文本后备模型 {TEXT_FALLBACK_MODEL} 返回错误{status}。"
            "请检查 TEXT_FALLBACK_MODEL、TEXT_FALLBACK_API_KEY、TEXT_FALLBACK_BASE_URL。"
        )
    return (
        f"DeepSeek 不可用，且百炼文本后备模型 {TEXT_FALLBACK_MODEL} 网络请求失败。"
        "请检查 TEXT_FALLBACK_BASE_URL、网络连接和 DASHSCOPE_API_KEY。"
    )


def _model_missing_error(has_images: bool) -> str:
    if has_images:
        return "图片问答模型未配置，请配置 VISION_MODEL / VISION_API_KEY 后重试。"
    return "文本问答模型未配置，请配置 DEEPSEEK_MODEL / DEEPSEEK_API_KEY 后重试。"


def _model_error_message(
    has_images: bool,
    reason: str,
    status_code: Optional[int] = None,
    model: str = "",
) -> str:
    model_label = model or ("图片模型" if has_images else "文本模型")
    if reason == "api":
        status = f"（HTTP {status_code}）" if status_code else ""
        if has_images:
            return (
                f"图片问答生成失败：{model_label} 返回错误{status}。"
                "请检查 VISION_MODEL、VISION_API_KEY、VISION_BASE_URL 和 OSS 签名 URL 是否可访问。"
            )
        return (
            f"回答生成失败：{model_label} 返回错误{status}。"
            "请检查 DEEPSEEK_MODEL、DEEPSEEK_API_KEY、DEEPSEEK_BASE_URL 和网络连接。"
        )
    if has_images:
        return (
            f"图片问答生成失败：{model_label} 网络请求失败。"
            "请检查 VISION_MODEL、VISION_API_KEY、VISION_BASE_URL 和 OSS 签名 URL 是否可访问。"
        )
    return (
        f"回答生成失败：{model_label} 网络请求失败。"
        "请检查 DEEPSEEK_MODEL、DEEPSEEK_API_KEY、DEEPSEEK_BASE_URL 和网络连接。"
    )


def _build_image_urls(attachments: list[dict]) -> list[str]:
    urls = []
    for item in attachments:
        object_key = item.get("object_key")
        if object_key:
            urls.append(_public_oss_url(object_key))
    return urls


# ---------------------------------------------------------------------------
# knowledge endpoints
# ---------------------------------------------------------------------------


@app.get("/api/knowledge")
def list_knowledge(knowledge_base_id: Optional[int] = None,
                   _user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    knowledge_base = _resolve_knowledge_base(db, knowledge_base_id)
    files = (
        db.query(KnowledgeFile)
        .filter_by(knowledge_base_id=knowledge_base.id)
        .order_by(KnowledgeFile.created_at.desc())
        .all()
    )
    return [
        {
            "id": f.id,
            "knowledge_base_id": f.knowledge_base_id,
            "name": f.name,
            "size": f.size,
            "created_at": f.created_at.isoformat() if f.created_at else "",
        }
        for f in files
    ]


@app.post("/api/knowledge/upload")
async def upload_knowledge(file: UploadFile = File(...),
                           knowledge_base_id: Optional[int] = Form(None),
                           _user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    knowledge_base = _resolve_knowledge_base(db, knowledge_base_id)
    content = await file.read()
    text = _extract_file_text(file.filename or "", content)

    entry = KnowledgeFile(
        knowledge_base_id=knowledge_base.id,
        name=file.filename or "unknown",
        size=len(content),
        content=text,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)

    chunks = _chunk_text(text, entry.id)
    from chroma_client import add_chunks, delete_file_chunks
    try:
        add_chunks(chunks, entry.id, entry.name, knowledge_base.id)
    except Exception as exc:
        logger.warning("Knowledge file indexing failed: file_id=%s error=%s", entry.id, exc, exc_info=True)
        try:
            delete_file_chunks(entry.id)
        except Exception as cleanup_exc:
            logger.warning(
                "Failed to clean partially indexed chunks: file_id=%s error=%s",
                entry.id,
                cleanup_exc,
                exc_info=True,
            )
        db.delete(entry)
        db.commit()
        raise HTTPException(500, "文件索引失败，已取消本次上传，请稍后重试")

    return {
        "id": entry.id,
        "knowledge_base_id": entry.knowledge_base_id,
        "name": entry.name,
        "size": entry.size,
        "created_at": entry.created_at.isoformat() if entry.created_at else "",
    }


@app.delete("/api/knowledge/{fid}")
def delete_knowledge(fid: int, _user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    entry = db.query(KnowledgeFile).filter_by(id=fid).first()
    if not entry:
        raise HTTPException(404, "文件不存在")
    from chroma_client import delete_file_chunks
    try:
        delete_file_chunks(fid)
    except Exception as exc:
        logger.warning("Knowledge file vector cleanup failed: file_id=%s error=%s", fid, exc, exc_info=True)
        raise HTTPException(500, "文件向量索引删除失败，已保留原文件，请稍后重试")
    db.delete(entry)
    db.commit()
    return {"message": "ok"}


@app.get("/api/knowledge/{fid}")
def get_knowledge_detail(fid: int, _user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    entry = db.query(KnowledgeFile).filter_by(id=fid).first()
    if not entry:
        raise HTTPException(404, "文件不存在")
    return {
        "id": entry.id,
        "knowledge_base_id": entry.knowledge_base_id,
        "name": entry.name,
        "size": entry.size,
        "created_at": entry.created_at.isoformat() if entry.created_at else "",
    }


@app.get("/api/knowledge/{fid}/content")
def get_knowledge_content(fid: int, _user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    entry = db.query(KnowledgeFile).filter_by(id=fid).first()
    if not entry:
        raise HTTPException(404, "文件不存在")
    content = entry.content or ""
    if not content and (entry.name or "").lower().endswith(".docx"):
        content = "该文件上传时未抽取内容，请重新上传以生成预览。"
    return {"id": entry.id, "name": entry.name, "content": content}


def _extract_file_text(filename: str, content: bytes) -> str:
    lower_name = filename.lower()
    if lower_name.endswith(".docx"):
        return _extract_docx_text(content)
    if lower_name.endswith(".pdf"):
        return _extract_pdf_text(content)
    return content.decode("utf-8", errors="replace")


def _extract_docx_text(content: bytes) -> str:
    from docx import Document

    try:
        document = Document(BytesIO(content))
    except Exception as exc:
        raise HTTPException(400, f"DOCX 解析失败：{exc}")
    parts = [p.text.strip() for p in document.paragraphs if p.text.strip()]

    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))

    return "\n".join(parts)


def _extract_pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise HTTPException(500, "后端缺少 pypdf 依赖，无法解析 PDF")

    try:
        reader = PdfReader(BytesIO(content))
        parts = []
        for index, page in enumerate(reader.pages, start=1):
            page_text = (page.extract_text() or "").strip()
            if page_text:
                parts.append(f"第 {index} 页\n{page_text}")
        return "\n\n".join(parts)
    except Exception as exc:
        raise HTTPException(400, f"PDF 解析失败：{exc}")


def _chunk_text(text: str, file_id: int, chunk_size: int = 500, chunk_overlap: int = 50) -> list[dict]:
    """Split text into chunks with overlap and return them with ids."""
    if not text.strip():
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk_text = text[start:end].strip()
        if chunk_text:
            chunks.append({"id": f"{start}", "text": chunk_text})
        start += (chunk_size - chunk_overlap)
    return chunks


# ---------------------------------------------------------------------------
# checkpointer endpoints (for frontend state management)
# ---------------------------------------------------------------------------


@app.get("/api/checkpointer/threads")
def list_checkpointer_threads(_user: User = Depends(get_current_user)):
    return {"threads": list_threads()}


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8002, reload=True)

