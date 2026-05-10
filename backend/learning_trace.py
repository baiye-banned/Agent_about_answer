import json
from datetime import datetime
from uuid import uuid4

from config import LEARNING_TRACE_ENABLED, LEARNING_TRACE_MAX_TEXT_CHARS
from database import SessionLocal
from models import ChatTraceSession


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clip_text(value: str, max_chars: int | None = None) -> str:
    limit = max_chars or LEARNING_TRACE_MAX_TEXT_CHARS
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "...(已截断)"


def sanitize_trace_value(value):
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(secret in lowered for secret in ["authorization", "api_key", "secret", "signature", "token"]):
                sanitized[key] = "***"
            else:
                sanitized[key] = sanitize_trace_value(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_trace_value(item) for item in value[:20]]
    if isinstance(value, str):
        return _clip_text(value)
    return value


def compact_trace_reference(trace: dict | None) -> dict:
    trace = trace or {}
    events = trace.get("events") or []
    return {
        "trace_id": trace.get("trace_id", ""),
        "status": trace.get("status", ""),
        "event_count": len(events),
        "last_stage": events[-1].get("stage", "") if events else "",
    }


def summarize_text(value: str, max_chars: int | None = None) -> str:
    return _clip_text(value, max_chars)


def summarize_messages(messages) -> list[dict]:
    rows = []
    for message in messages:
        rows.append({
            "id": getattr(message, "id", None),
            "role": getattr(message, "role", ""),
            "content": summarize_text(getattr(message, "content", "") or "", 260),
        })
    return rows


class TraceRecorder:
    def __init__(self, user_id: int | None = None):
        self.enabled = LEARNING_TRACE_ENABLED
        self.trace_id = uuid4().hex[:16]
        self.user_id = user_id
        self.events: list[dict] = []
        self.status = "running"
        self._cursor = 0
        if self.enabled:
            self._persist()

    def add(
        self,
        stage: str,
        function: str,
        *,
        creates: dict | None = None,
        uses: dict | None = None,
        params: dict | None = None,
        result: dict | None = None,
        note: str = "",
    ) -> dict:
        if not self.enabled:
            return {}
        event = {
            "index": len(self.events) + 1,
            "time": _now_iso(),
            "stage": stage,
            "function": function,
            "creates": sanitize_trace_value(creates or {}),
            "uses": sanitize_trace_value(uses or {}),
            "params": sanitize_trace_value(params or {}),
            "result": sanitize_trace_value(result or {}),
            "note": note,
        }
        self.events.append(event)
        self._safe_persist()
        return event

    def finish(self, status: str = "done", **extra):
        if not self.enabled:
            return
        self.status = status
        self._safe_persist(**extra)

    def attach(self, conversation_id: str | None = None, message_id: int | None = None, status: str | None = None):
        if status:
            self.status = status
        if self.enabled:
            self._safe_persist(conversation_id=conversation_id, message_id=message_id)

    def snapshot(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "status": self.status,
            "events": self.events,
        }

    def drain_sse_payloads(self) -> list[dict]:
        if not self.enabled:
            return []
        payloads = []
        while self._cursor < len(self.events):
            event = self.events[self._cursor]
            payloads.append({
                "type": "trace",
                "trace_id": self.trace_id,
                "event": event,
            })
            self._cursor += 1
        return payloads

    def _persist(self, conversation_id: str | None = None, message_id: int | None = None):
        db = SessionLocal()
        try:
            session = db.query(ChatTraceSession).filter_by(id=self.trace_id).first()
            if not session:
                session = ChatTraceSession(id=self.trace_id, user_id=self.user_id)
                db.add(session)
            if conversation_id is not None:
                session.conversation_id = conversation_id
            if message_id is not None:
                session.message_id = message_id
            session.status = self.status
            session.events = json.dumps(self.events, ensure_ascii=False)
            db.commit()
        finally:
            db.close()

    def _safe_persist(self, **kwargs):
        try:
            self._persist(**kwargs)
        except Exception:
            # Trace must never break the primary streaming answer path.
            pass


def append_trace_event(trace_id: str | None, stage: str, function: str, **kwargs):
    if not trace_id or not LEARNING_TRACE_ENABLED:
        return
    db = SessionLocal()
    try:
        session = db.query(ChatTraceSession).filter_by(id=trace_id).first()
        if not session:
            return
        events = json.loads(session.events or "[]")
        event = {
            "index": len(events) + 1,
            "time": _now_iso(),
            "stage": stage,
            "function": function,
            "creates": sanitize_trace_value(kwargs.get("creates") or {}),
            "uses": sanitize_trace_value(kwargs.get("uses") or {}),
            "params": sanitize_trace_value(kwargs.get("params") or {}),
            "result": sanitize_trace_value(kwargs.get("result") or {}),
            "note": kwargs.get("note", ""),
        }
        events.append(event)
        session.events = json.dumps(events, ensure_ascii=False)
        if kwargs.get("status"):
            session.status = kwargs["status"]
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


def serialize_trace_session(session: ChatTraceSession) -> dict:
    try:
        events = json.loads(session.events or "[]")
    except json.JSONDecodeError:
        events = []
    return {
        "trace_id": session.id,
        "user_id": session.user_id,
        "conversation_id": session.conversation_id,
        "message_id": session.message_id,
        "status": session.status,
        "events": events,
        "created_at": session.created_at.isoformat() if session.created_at else "",
        "updated_at": session.updated_at.isoformat() if session.updated_at else "",
    }
