import json

from sqlalchemy.orm import Session

from database.session import SessionLocal
from model.models import ChatTraceSession, Conversation


def persist_trace_session(
    trace_id: str,
    *,
    user_id: int | None,
    status: str,
    events: list[dict],
    conversation_id: str | None = None,
    message_id: int | None = None,
):
    db = SessionLocal()
    try:
        session = db.query(ChatTraceSession).filter_by(id=trace_id).first()
        if not session:
            session = ChatTraceSession(id=trace_id, user_id=user_id)
            db.add(session)
        if conversation_id is not None:
            session.conversation_id = conversation_id
        if message_id is not None:
            session.message_id = message_id
        session.status = status
        session.events = json.dumps(events, ensure_ascii=False)
        db.commit()
    finally:
        db.close()


def append_trace_event(trace_id: str, event: dict, status: str | None = None):
    db = SessionLocal()
    try:
        session = db.query(ChatTraceSession).filter_by(id=trace_id).first()
        if not session:
            return
        events = _load_events(session.events)
        events.append(event)
        session.events = json.dumps(events, ensure_ascii=False)
        if status:
            session.status = status
        db.commit()
    finally:
        db.close()


def get_trace_snapshot(trace_id: str, user_id: int | None = None) -> dict | None:
    db = SessionLocal()
    try:
        query = db.query(ChatTraceSession).filter_by(id=trace_id)
        if user_id is not None:
            query = query.filter_by(user_id=user_id)
        session = query.first()
        if not session:
            return None
        if not _conversation_alive(db, session.conversation_id):
            return None
        return serialize_trace_session(session)
    finally:
        db.close()


def _conversation_alive(db: Session, conversation_id: str | None) -> bool:
    """轨迹所属会话还在才可读（issue #127）。

    删除会话时轨迹行已随会话在同一个事务里删掉（`delete_trace_sessions_for_conversation`）；
    这里兜的是两类没被那条路径覆盖的行：修复前删掉的会话留下的历史孤儿行（删除链路上的
    清理追不回来），以及会话删除与「同一轮问答的流还没结束」交错时被
    `persist_trace_session` 重新写回去的行。conversation_id 为空的轨迹不属于任何会话，
    不受这条约束。
    """
    if not conversation_id:
        return True
    return db.query(Conversation.id).filter(Conversation.id == conversation_id).first() is not None


def delete_trace_sessions_for_conversation(db: Session, conversation_id: str) -> int:
    """删除某个会话下的全部学习轨迹行，返回删除条数。

    `chat_trace_sessions.conversation_id` 是没有外键约束的普通列（model/models.py），
    数据库不会随会话级联，必须由应用层按会话显式清理。

    用调用方传进来的 `db` 而不是自开 SessionLocal：清理要与会话删除落在**同一个事务**里，
    否则「先提交会话删除、再删轨迹」一旦中途失败，就留下「会话已删、轨迹仍能读」的残留。
    """
    return (
        db.query(ChatTraceSession)
        .filter(ChatTraceSession.conversation_id == conversation_id)
        .delete(synchronize_session=False)
    )


def get_trace_session_by_message(db: Session, message_id: int, user_id: int) -> ChatTraceSession | None:
    return db.query(ChatTraceSession).filter_by(message_id=message_id, user_id=user_id).first()


def serialize_trace_session(session: ChatTraceSession) -> dict:
    return {
        "trace_id": session.id,
        "user_id": session.user_id,
        "conversation_id": session.conversation_id,
        "message_id": session.message_id,
        "status": session.status,
        "events": _load_events(session.events),
        "created_at": session.created_at.isoformat() if session.created_at else "",
        "updated_at": session.updated_at.isoformat() if session.updated_at else "",
    }


def _load_events(raw_events: str | list | None) -> list[dict]:
    try:
        events = raw_events if isinstance(raw_events, list) else json.loads(raw_events or "[]")
        if not isinstance(events, list):
            return []
        return [event for event in events if isinstance(event, dict)]
    except (TypeError, json.JSONDecodeError):
        return []

