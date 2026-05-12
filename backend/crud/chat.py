import json

from sqlalchemy.orm import Session

from model.models import ChatTraceSession, Conversation, Message


def serialize_message(message: Message) -> dict:
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


def list_conversations(db: Session, user_id: int) -> list[Conversation]:
    return (
        db.query(Conversation)
        .filter_by(user_id=user_id)
        .order_by(Conversation.updated_at.desc())
        .all()
    )


def get_conversation(db: Session, cid: str, user_id: int) -> Conversation | None:
    return db.query(Conversation).filter_by(id=cid, user_id=user_id).first()


def list_messages(db: Session, cid: str, user_id: int) -> list[Message] | None:
    conversation = get_conversation(db, cid, user_id)
    if not conversation:
        return None
    return list(conversation.messages)


def delete_conversation(db: Session, cid: str, user_id: int) -> Conversation | None:
    conversation = get_conversation(db, cid, user_id)
    if not conversation:
        return None
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


def _loads_json(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default
