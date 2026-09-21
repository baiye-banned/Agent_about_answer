from sqlalchemy.orm import Session, selectinload

from crud import trace as crud_trace
from model.models import ChatTraceSession, Conversation, Message
from service.json_utils import load_json_value


# 消息历史默认页大小与单页上限：不传分页参数时也必须有上限，否则会话越长单次响应越大。
CHAT_MESSAGE_DEFAULT_LIMIT = 50
CHAT_MESSAGE_MAX_LIMIT = 200


def serialize_message(message: Message) -> dict:
    retrieval_trace = load_json_value(message.retrieval_trace, {})
    if not isinstance(retrieval_trace, dict):
        retrieval_trace = {}

    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "sources": load_json_value(message.sources, []),
        "attachments": load_json_value(message.attachments, []),
        "ragas_status": message.ragas_status or "",
        "ragas_scores": load_json_value(message.ragas_scores, {}),
        "ragas_error": message.ragas_error or "",
        "retrieval_trace": retrieval_trace,
        "image_analysis_status": retrieval_trace.get("image_analysis_status", ""),
        "image_analysis_error": retrieval_trace.get("image_analysis_error", ""),
        "image_description": retrieval_trace.get("image_description", ""),
        "created_at": message.created_at.isoformat() if message.created_at else "",
    }


def list_conversations(db: Session, user_id: int) -> list[Conversation]:
    # 预加载知识库：序列化时每个会话都要读 conv.knowledge_base，逐行懒加载会让查询数随会话数增长。
    return (
        db.query(Conversation)
        .options(selectinload(Conversation.knowledge_base))
        .filter_by(user_id=user_id)
        .order_by(Conversation.updated_at.desc())
        .all()
    )


def get_conversation(db: Session, cid: str, user_id: int) -> Conversation | None:
    return db.query(Conversation).filter_by(id=cid, user_id=user_id).first()


def list_messages(
    db: Session,
    cid: str,
    user_id: int,
    *,
    limit: int = CHAT_MESSAGE_DEFAULT_LIMIT,
    before_id: int | None = None,
) -> list[Message] | None:
    """按会话取一页消息，返回「旧 -> 新」顺序，与旧接口的数组顺序一致。

    游标用自增主键而不是 created_at：created_at 是秒级 DATETIME，同一秒内的消息按它排序
    不稳定，翻页会重复或漏行。先倒序取下 limit 条再反转，取到的就是 cursor 之前最新的那页。

    页大小在这里兜底夹取（而不是只依赖服务层的 422 校验）：CRUD 是更底层的入口，脚本或内部
    调用可能绕开接口层，负值在 SQLite 上等价于「不设上限」，会把整段历史一次读出来。
    """
    conversation = get_conversation(db, cid, user_id)
    if not conversation:
        return None
    limit = CHAT_MESSAGE_DEFAULT_LIMIT if limit is None else max(1, min(limit, CHAT_MESSAGE_MAX_LIMIT))
    query = db.query(Message).filter(Message.conversation_id == cid)
    if before_id is not None:
        query = query.filter(Message.id < before_id)
    rows = query.order_by(Message.id.desc()).limit(limit).all()
    rows.reverse()
    return rows


def delete_conversation(db: Session, cid: str, user_id: int) -> Conversation | None:
    conversation = get_conversation(db, cid, user_id)
    if not conversation:
        return None
    # 学习轨迹按 conversation_id 关联，列上没有外键约束（model/models.py），数据库不会级联，
    # 必须显式清理。与会话删除共用同一次提交：分两次提交时中间失败就会留下
    # 「会话已删、轨迹仍能读」的残留（issue #127）。不按 LEARNING_TRACE_ENABLED 分流——
    # 关掉开关只是不再写新轨迹，已经写下的行仍然要随会话删除。
    crud_trace.delete_trace_sessions_for_conversation(db, cid)
    db.delete(conversation)
    db.commit()
    return conversation


def rename_conversation(db: Session, cid: str, user_id: int, title: str) -> Conversation | None:
    conversation = get_conversation(db, cid, user_id)
    if not conversation:
        return None
    conversation.title = title
    db.commit()
    db.refresh(conversation)
    return conversation


def get_message_by_user(db: Session, message_id: int, user_id: int) -> Message | None:
    return (
        db.query(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .filter(Message.id == message_id, Conversation.user_id == user_id)
        .first()
    )


def get_trace_session_by_message(db: Session, message_id: int, user_id: int) -> ChatTraceSession | None:
    return db.query(ChatTraceSession).filter_by(message_id=message_id, user_id=user_id).first()
