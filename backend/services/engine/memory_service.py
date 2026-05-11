
import asyncio
import logging
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_MODEL,
    MEMORY_RECENT_MAX_CHARS,
    MEMORY_SUMMARY_MAX_CHARS,
    MEMORY_SUMMARY_TRIGGER_TURNS,
    MEMORY_WINDOW_TURNS,
)
from database import SessionLocal
from learning_trace import append_trace_event
from models import Conversation, Message
from services.base.utils_service import _clip_text
from tool import deepseek_chat_url, normalize_deepseek_model


logger = logging.getLogger(__name__)


def _build_memory_context(db: Session, conversation: Conversation, current_message_id: int) -> str:
    summary = (conversation.memory_summary or "").strip()
    summary_upto = conversation.memory_summary_upto_message_id or 0
    recent_messages = (
        db.query(Message)
        .filter(
            Message.conversation_id == conversation.id,
            Message.id < current_message_id,
            Message.id > summary_upto,
        )
        .order_by(Message.id.desc())
        .limit(max(MEMORY_WINDOW_TURNS, 1) * 2)
        .all()
    )
    recent_text = _format_recent_memory_messages(list(reversed(recent_messages)))

    sections = []
    if summary:
        sections.append(f"长期摘要记忆：\n{_clip_text(summary, MEMORY_SUMMARY_MAX_CHARS)}")
    if recent_text:
        sections.append(f"最近对话窗口：\n{recent_text}")
    return "\n\n".join(sections).strip()


def _build_memory_aware_retrieval_question(question: str, memory_context: str) -> str:
    if not memory_context:
        return question
    compact_memory = _clip_text(memory_context, 1500)
    return (
        "以下会话记忆仅用于消解当前问题中的指代和省略，不作为事实依据。\n"
        f"{compact_memory}\n\n"
        f"当前检索问题：{question}"
    )


def _format_recent_memory_messages(messages: list[Message]) -> str:
    lines = []
    for message in messages:
        content = (message.content or "").strip()
        if not content:
            continue
        role = "用户" if message.role == "user" else "助手"
        lines.append(f"{role}：{content}")

    selected = []
    total = 0
    for line in reversed(lines):
        line_length = len(line)
        if selected and total + line_length > MEMORY_RECENT_MAX_CHARS:
            break
        if line_length > MEMORY_RECENT_MAX_CHARS:
            line = _clip_text(line, MEMORY_RECENT_MAX_CHARS)
            line_length = len(line)
        selected.append(line)
        total += line_length
    return "\n".join(reversed(selected))


def _schedule_memory_summary_update(conversation_id: str, trace_id: str | None = None):
    try:
        asyncio.create_task(_maybe_update_memory_summary(conversation_id, trace_id))
    except RuntimeError:
        logger.warning("Unable to schedule memory summary update: conversation_id=%s", conversation_id)
        append_trace_event(
            trace_id,
            "memory_summary_schedule_failed",
            "_schedule_memory_summary_update",
            result={"conversation_id": conversation_id},
            note="当前事件循环不可用，摘要压缩检查没有成功启动。",
        )


async def _maybe_update_memory_summary(conversation_id: str, trace_id: str | None = None):
    db = SessionLocal()
    try:
        conversation = db.query(Conversation).filter_by(id=conversation_id).first()
        if not conversation:
            append_trace_event(
                trace_id,
                "memory_summary_skipped",
                "_maybe_update_memory_summary",
                result={"reason": "conversation_not_found"},
                note="摘要检查跳过：会话不存在。",
            )
            return

        assistant_messages = (
            db.query(Message)
            .filter_by(conversation_id=conversation_id, role="assistant")
            .order_by(Message.id.asc())
            .all()
        )
        if len(assistant_messages) <= MEMORY_SUMMARY_TRIGGER_TURNS:
            append_trace_event(
                trace_id,
                "memory_summary_skipped",
                "_maybe_update_memory_summary",
                result={
                    "assistant_turns": len(assistant_messages),
                    "trigger_turns": MEMORY_SUMMARY_TRIGGER_TURNS,
                    "reason": "turns_not_enough",
                },
                note="摘要检查跳过：完整回答轮数还没有超过触发阈值。",
            )
            return

        cutoff_index = len(assistant_messages) - max(MEMORY_WINDOW_TURNS, 1) - 1
        if cutoff_index < 0:
            append_trace_event(
                trace_id,
                "memory_summary_skipped",
                "_maybe_update_memory_summary",
                result={"reason": "no_window_outside_messages"},
                note="摘要检查跳过：窗口外没有足够历史消息可压缩。",
            )
            return
        cutoff_message = assistant_messages[cutoff_index]
        summary_upto = conversation.memory_summary_upto_message_id or 0
        unsummarized_turns = [
            message for message in assistant_messages
            if summary_upto < message.id <= cutoff_message.id
        ]
        if len(unsummarized_turns) < MEMORY_WINDOW_TURNS:
            append_trace_event(
                trace_id,
                "memory_summary_skipped",
                "_maybe_update_memory_summary",
                result={
                    "unsummarized_turns": len(unsummarized_turns),
                    "required_turns": MEMORY_WINDOW_TURNS,
                    "summary_upto_message_id": summary_upto,
                },
                note="摘要检查跳过：距离上次摘要之后新增的窗口外轮数不足，避免每轮都调用模型摘要。",
            )
            return

        messages_to_summarize = (
            db.query(Message)
            .filter(
                Message.conversation_id == conversation_id,
                Message.id > summary_upto,
                Message.id <= cutoff_message.id,
            )
            .order_by(Message.id.asc())
            .all()
        )
        transcript = _format_messages_for_summary(messages_to_summarize)
        if not transcript:
            append_trace_event(
                trace_id,
                "memory_summary_skipped",
                "_maybe_update_memory_summary",
                result={"reason": "empty_transcript"},
                note="摘要检查跳过：待摘要消息内容为空。",
            )
            return

        append_trace_event(
            trace_id,
            "memory_summary_triggered",
            "_maybe_update_memory_summary",
            params={
                "assistant_turns": len(assistant_messages),
                "trigger_turns": MEMORY_SUMMARY_TRIGGER_TURNS,
                "unsummarized_turns": len(unsummarized_turns),
                "cutoff_message_id": cutoff_message.id,
            },
            creates={"transcript": transcript},
            note="摘要条件满足，系统将窗口外历史和旧摘要合并压缩为新的长期记忆。",
        )
        next_summary = await _summarize_conversation_memory(
            conversation.memory_summary or "",
            transcript,
        )
        if not next_summary:
            append_trace_event(
                trace_id,
                "memory_summary_failed",
                "_summarize_conversation_memory",
                result={"reason": "empty_summary_or_model_failed"},
                note="摘要模型没有返回可用摘要，主回答不受影响，保留原摘要。",
            )
            return

        conversation.memory_summary = _clip_text(next_summary, MEMORY_SUMMARY_MAX_CHARS)
        conversation.memory_summary_upto_message_id = cutoff_message.id
        conversation.memory_updated_at = datetime.now()
        db.commit()
        append_trace_event(
            trace_id,
            "memory_summary_updated",
            "_summarize_conversation_memory",
            creates={
                "memory_summary": conversation.memory_summary,
                "memory_summary_upto_message_id": cutoff_message.id,
            },
            note="长期摘要已更新。后续追问会同时使用这份摘要和最近 4 轮滑动窗口。",
        )
        logger.info(
            "Conversation memory summary updated: conversation_id=%s upto_message_id=%s",
            conversation_id,
            cutoff_message.id,
        )
    except Exception as exc:
        logger.warning(
            "Conversation memory summary update failed: conversation_id=%s error=%s",
            conversation_id,
            exc,
            exc_info=True,
        )
        append_trace_event(
            trace_id,
            "memory_summary_failed",
            "_maybe_update_memory_summary",
            result={"error": str(exc)},
            note="摘要压缩发生异常，但它是异步辅助能力，不会影响本轮回答。",
        )
    finally:
        db.close()


def _format_messages_for_summary(messages: list[Message]) -> str:
    lines = []
    for message in messages:
        content = _clip_text((message.content or "").strip(), 1200)
        if not content:
            continue
        role = "用户" if message.role == "user" else "助手"
        lines.append(f"{role}：{content}")
    return "\n".join(lines)


async def _summarize_conversation_memory(previous_summary: str, transcript: str) -> str:
    if not DEEPSEEK_API_KEY:
        logger.warning("Skip memory summary update because DeepSeek API Key is not configured")
        return ""

    system_prompt = (
        "你是企业知识库问答系统的会话记忆压缩器。"
        "请把旧摘要和新增对话压缩成可用于后续追问理解的中文摘要。"
        "只保留用户目标、当前任务背景、用户明确确认过的事实、关键约束/偏好/口径、未解决问题或待办。"
        "不要保存知识库原文大段内容，不要编造未确认事实，不要输出 Markdown 标题。"
    )
    user_prompt = (
        f"摘要长度上限：{MEMORY_SUMMARY_MAX_CHARS} 字。\n\n"
        f"旧摘要：\n{previous_summary or '（无）'}\n\n"
        f"新增对话：\n{transcript}\n\n"
        "请输出新的会话摘要："
    )
    payload = {
        "model": normalize_deepseek_model(DEEPSEEK_MODEL),
        "stream": False,
        "temperature": 0.1,
        "max_tokens": 900,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(deepseek_chat_url(), json=payload, headers=headers)
        if response.status_code >= 400:
            logger.warning(
                "Memory summary call failed: status=%s detail=%s",
                response.status_code,
                response.text[:300],
            )
            return ""
        content = response.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        return _clip_text(content.strip(), MEMORY_SUMMARY_MAX_CHARS)
    except Exception as exc:
        logger.warning("Memory summary network request failed: %s", exc, exc_info=True)
        return ""
