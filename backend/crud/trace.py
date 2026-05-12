import json

from sqlalchemy.orm import Session

from database.session import SessionLocal
from model.models import ChatTraceSession


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
        return serialize_trace_session(session)
    finally:
        db.close()


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


def _load_events(raw_events: str | None) -> list[dict]:
    try:
        events = json.loads(raw_events or "[]")
        return events if isinstance(events, list) else []
    except json.JSONDecodeError:
        return []

