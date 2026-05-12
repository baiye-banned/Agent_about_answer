
import json
import logging

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from database.checkpointer import list_threads
from crud import chat as crud_chat
from database.session import get_db
from rag.learning_trace import TraceRecorder, get_trace_snapshot, serialize_trace_session as _serialize_trace_session
from model.models import User
from service.auth_service import get_current_user
from service.utils_service import _loads_json


logger = logging.getLogger(__name__)


def _trace_sse_payloads(trace: TraceRecorder) -> list[str]:
    return [
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        for payload in trace.drain_sse_payloads()
    ]


def _safe_trace_add(trace: TraceRecorder, *args, **kwargs):
    try:
        return trace.add(*args, **kwargs)
    except Exception as exc:
        logger.warning("Learning trace add failed: %s", exc, exc_info=True)
        return {}


def _safe_trace_finish(trace: TraceRecorder, *args, **kwargs):
    try:
        trace.finish(*args, **kwargs)
    except Exception as exc:
        logger.warning("Learning trace finish failed: %s", exc, exc_info=True)


def _safe_trace_attach(trace: TraceRecorder, *args, **kwargs):
    try:
        trace.attach(*args, **kwargs)
    except Exception as exc:
        logger.warning("Learning trace attach failed: %s", exc, exc_info=True)


def get_chat_trace(trace_id: str, user: User = Depends(get_current_user)):
    trace = get_trace_snapshot(trace_id, user_id=user.id)
    if not trace:
        raise HTTPException(404, "Trace 不存在")
    return trace


def get_message_trace(message_id: int, user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    message = crud_chat.get_message_by_user(db, message_id, user.id)
    if not message:
        raise HTTPException(404, "消息不存在")

    retrieval_trace = _loads_json(message.retrieval_trace, {})
    learning_trace = retrieval_trace.get("learning_trace", {}) if isinstance(retrieval_trace, dict) else {}
    trace_id = learning_trace.get("trace_id")
    if trace_id:
        trace = get_trace_snapshot(trace_id, user_id=user.id)
        if trace:
            return trace

    trace_session = crud_chat.get_trace_session_by_message(db, message_id, user.id)
    if trace_session:
        return _serialize_trace_session(trace_session)
    if learning_trace:
        return learning_trace
    raise HTTPException(404, "该消息暂无流程 Trace")



def list_checkpointer_threads(_user: User = Depends(get_current_user)):
    return {"threads": list_threads()}
