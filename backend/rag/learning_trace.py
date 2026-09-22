import asyncio
from datetime import datetime
from uuid import uuid4

from config import LEARNING_TRACE_ENABLED, LEARNING_TRACE_MAX_TEXT_CHARS
from crud import trace as crud_trace
from model.models import ChatTraceSession


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clip_text(value: str, max_chars: int | None = None) -> str:
    limit = max_chars or LEARNING_TRACE_MAX_TEXT_CHARS
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "...(已截断)"


FULL_TEXT_TRACE_KEYS = {
    "effective_question",
    "recent_text",
    "memory_context",
    "memory_summary",
    "conv.memory_summary",
    "retrieval_question",
    "transcript",
}


def sanitize_trace_value(value, key: str | None = None):
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(secret in lowered for secret in ["authorization", "api_key", "secret", "signature", "token"]):
                sanitized[key] = "***"
            else:
                sanitized[key] = sanitize_trace_value(item, key=str(key))
        return sanitized
    if isinstance(value, list):
        return [sanitize_trace_value(item, key=key) for item in value[:20]]
    if isinstance(value, str):
        if key in FULL_TEXT_TRACE_KEYS:
            return value
        return _clip_text(value)
    return value


def _trace_object(value):
    return {} if value is None else value


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
            "content": summarize_text(getattr(message, "content", ""), 260),
        })
    return rows


class TraceRecorder:
    """一次请求的学习轨迹：事件攒在内存里，每加一条就把整份快照写回 `chat_trace_sessions`。

    **写回一律走工作线程**（issue #201）。`add` / `attach` / `finish` 都是协程，调用方必须
    `await`：每个方法只做内存里的记账（µs 级），写库那一段交给 `asyncio.to_thread`，于是
    「开 session → 用 → 关」整段落在同一个工作线程上——`crud/trace.py` 自己开的那个同步
    Session 既不在事件循环线程上，也不跨线程传递。这与 `rag/memory_service.py`、
    `service/chat_service.py` 在 issue #187 里定下的口径是同一条（`retrieval.py:163-166`
    是本仓对该形态的成文规范）。

    顺序由「每步都 await」保证：调用方在前一条写回完成之前拿不到下一条，事件既不会乱序，
    也不会丢。取消（客户端断连）不改变这件事——写入已经被交给工作线程，即使调用方在
    `await` 处被打断，那一段也会跑完并关掉自己的会话。

    构造器**不写库**：它在事件循环线程上被调用（`stream_chat` 第一句），落首行交给随后的
    第一次 `await add/attach/finish`；`persist_trace_session` 本就是「没有就建、有就更新」，
    所以首行出现的时点与原先（构造时）只差一次 await。
    """

    def __init__(self, user_id: int | None = None):
        self.enabled = LEARNING_TRACE_ENABLED
        self.trace_id = uuid4().hex[:16]
        self.user_id = user_id
        self.events: list[dict] = []
        self.status = "running"
        self._cursor = 0

    async def add(
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
            "creates": sanitize_trace_value(_trace_object(creates)),
            "uses": sanitize_trace_value(_trace_object(uses)),
            "params": sanitize_trace_value(_trace_object(params)),
            "result": sanitize_trace_value(_trace_object(result)),
            "note": note,
        }
        self.events.append(event)
        await self._safe_persist()
        return event

    async def finish(self, status: str = "done", **extra):
        if not self.enabled:
            return
        self.status = status
        await self._safe_persist(**extra)

    async def attach(self, conversation_id: str | None = None, message_id: int | None = None, status: str | None = None):
        if status:
            self.status = status
        if self.enabled:
            await self._safe_persist(conversation_id=conversation_id, message_id=message_id)

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
        """同步写回一次快照：**这是交给工作线程的载荷**，绝不能在事件循环线程上直接调。

        读 `self.events` 的那一刻调用方都停在 `await` 上，不存在并发改这份列表的写入者。
        """
        crud_trace.persist_trace_session(
            self.trace_id,
            user_id=self.user_id,
            status=self.status,
            events=self.events,
            conversation_id=conversation_id,
            message_id=message_id,
        )

    async def _safe_persist(self, **kwargs):
        try:
            await asyncio.to_thread(self._persist, **kwargs)
        except Exception:
            # Trace must never break the primary streaming answer path.
            # 只吞 Exception：CancelledError/GeneratorExit 是 BaseException，断连语义不受影响。
            pass


def append_trace_event(trace_id: str | None, stage: str, function: str, **kwargs):
    """往一个已有 trace_id 上补一条事件（同步实现）。

    **只在工作线程或无事件循环的上下文里直接调用**：它经 `crud_trace.append_trace_event`
    自开一个同步 Session。事件循环线程上的调用方（`rag/memory_service.py`、
    `rag/ragas_eval.py` 的协程）必须把它整段交给 `asyncio.to_thread` 承载（issue #201），
    否则一次写回就独占事件循环。已经在工作线程里跑的调用方（`_evaluate_message_sync`）
    与「当前事件循环不可用」的兜底分支保持直接调用。
    """
    if not trace_id or not LEARNING_TRACE_ENABLED:
        return
    # 这里不能再用 get_trace_snapshot 取「已有事件数」：那条是读取面，带会话存活守卫
    # （issue #127），会话被删后对被写回的行返回 None，会把序号从 1 重排（issue #141）。
    # 序号交给 crud 在写事务内按行上已有条数分配。
    event = {
        "time": _now_iso(),
        "stage": stage,
        "function": function,
        "creates": sanitize_trace_value(_trace_object(kwargs.get("creates"))),
        "uses": sanitize_trace_value(_trace_object(kwargs.get("uses"))),
        "params": sanitize_trace_value(_trace_object(kwargs.get("params"))),
        "result": sanitize_trace_value(_trace_object(kwargs.get("result"))),
        "note": kwargs.get("note", ""),
    }
    crud_trace.append_trace_event(trace_id, event, status=kwargs.get("status"))


def get_trace_snapshot(trace_id: str, user_id: int | None = None) -> dict | None:
    return crud_trace.get_trace_snapshot(trace_id, user_id=user_id)


def serialize_trace_session(session: ChatTraceSession) -> dict:
    return crud_trace.serialize_trace_session(session)
