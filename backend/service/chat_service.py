
import json
import logging
import mimetypes
from datetime import datetime
from uuid import uuid4

from fastapi import Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from database.checkpointer import delete_thread_checkpoints
from config import MEMORY_WINDOW_TURNS
from crud import chat as crud_chat
from database.session import SessionLocal, get_db
from rag.learning_trace import TraceRecorder, compact_trace_reference, summarize_messages, summarize_text
from model.models import Conversation, Message, User, _new_id
from rag.ragas_eval import schedule_ragas_evaluation
from schema.schemas import ChatRequest, RenameRequest
from service.auth_service import decode_token, get_current_user
from service.oss_service import _public_oss_url, _put_oss_object
from service.trace_service import _safe_trace_add, _safe_trace_attach, _safe_trace_finish, _trace_sse_payloads
from service.utils_service import _build_sources
from service.knowledge_service import agentic_retrieve_knowledge, resolve_knowledge_base
from rag.memory_service import (
    _build_memory_aware_retrieval_question,
    _build_memory_context,
    _build_recent_memory_text,
    _schedule_memory_summary_update,
)
from rag.vision_service import _build_effective_question
from rag.chroma_client import embedding_backend_status
from rag.chains import stream_rag_answer
from tool.tools import decide_need_rag


logger = logging.getLogger(__name__)


def list_conversations(user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    rows = crud_chat.list_conversations(db, user.id)
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


def get_messages(cid: str, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    conv = crud_chat.get_conversation(db, cid, user.id)
    if not conv:
        raise HTTPException(404, "对话不存在")
    return [crud_chat.serialize_message(m) for m in conv.messages]


def delete_conversation(cid: str, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    conv = crud_chat.delete_conversation(db, cid, user.id)
    if not conv:
        raise HTTPException(404, "对话不存在")
    # clean checkpointer state
    delete_thread_checkpoints(cid)
    return {"message": "ok"}


def rename_conversation(cid: str, body: RenameRequest, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    conv = crud_chat.rename_conversation(db, cid, user.id, body.title)
    if not conv:
        raise HTTPException(404, "对话不存在")
    return {"message": "ok"}


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


async def stream_chat(body: ChatRequest, authorization: str = Header("")):
    username = decode_token(authorization)
    db = SessionLocal()
    user = db.query(User).filter_by(username=username).first()
    if not user:
        db.close()
        raise HTTPException(401, "User not found")
    trace = TraceRecorder(user_id=user.id)
    try:
        trace.add(
            "request_received",
            "stream_chat",
            creates={"trace_id": trace.trace_id},
            params={
                "conversation_id": body.conversation_id,
                "knowledge_base_id": body.knowledge_base_id,
                "question": body.question,
                "attachments_count": len(body.attachments or []),
            },
            result={"username": username},
            note="后端收到一次聊天请求，先建立 trace_id，后续所有步骤都会挂到这次请求下面。",
        )
        cid = body.conversation_id
        knowledge_base = resolve_knowledge_base(db, body.knowledge_base_id)
        raw_question = (body.question or "").strip()
        display_question = raw_question or ("请分析这张图片" if body.attachments else "")
        if not display_question:
            trace.add(
                "request_rejected",
                "stream_chat",
                uses={"raw_question": raw_question, "attachments_count": len(body.attachments or [])},
                result={"error": "问题不能为空"},
                note="没有文字问题，也没有图片附件，无法继续进入 RAG 流程。",
            )
            trace.finish("failed")
            raise HTTPException(400, "问题不能为空")
        trace.add(
            "input_normalized",
            "stream_chat",
            creates={"raw_question": raw_question, "display_question": display_question},
            result={"knowledge_base_id": knowledge_base.id, "knowledge_base_name": knowledge_base.name},
            note="系统整理用户输入，并确定本次请求要使用哪个知识库。",
        )

        effective_question, image_analysis = await _build_effective_question(
            raw_question,
            body.attachments,
        )
        trace.add(
            "effective_question_built",
            "_build_effective_question",
            params={"raw_question": raw_question, "attachments_count": len(body.attachments or [])},
            creates={
                "effective_question": effective_question,
                "image_analysis_status": image_analysis.get("status", ""),
                "image_description": image_analysis.get("description", ""),
            },
            result={"image_analysis_error": image_analysis.get("error", "")},
            note="如果有图片，系统会先把图片转成文字描述，再与用户问题合并为真正用于检索和生成的问题。",
        )
        if body.attachments and image_analysis.get("status") == "failed" and not raw_question:
            trace.add(
                "image_failed_directly",
                "_build_effective_question",
                uses={"attachments_count": len(body.attachments or [])},
                result={"error": image_analysis.get("error", "")},
                note="用户只发了图片但图片识别失败，因此不会进入知识库检索和模型回答。",
            )
            trace.finish("failed")
            db.close()

            async def failure_stream():
                for payload in _trace_sse_payloads(trace):
                    yield payload
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
            trace.add(
                "conversation_created",
                "stream_chat",
                creates={"conversation_id": cid, "title": title},
                result={"knowledge_base_id": knowledge_base.id},
                note="这是新对话，系统创建 conversation，并把它绑定到当前知识库。",
            )
        else:
            knowledge_base = conv.knowledge_base or knowledge_base
            trace.add(
                "conversation_loaded",
                "stream_chat",
                uses={"conversation_id": cid},
                result={"knowledge_base_id": knowledge_base.id, "title": conv.title},
                note="这是已有对话，系统复用它原本绑定的知识库，避免会话中途串库。",
            )
        trace.attach(conversation_id=cid)

        # save user message
        user_message = Message(
            conversation_id=cid,
            role="user",
            content=display_question,
            attachments=json.dumps(body.attachments, ensure_ascii=False),
        )
        db.add(user_message)
        db.commit()
        db.refresh(user_message)
        trace.add(
            "user_message_saved",
            "Message",
            creates={"user_message_id": user_message.id},
            params={"content": display_question, "attachments_count": len(body.attachments or [])},
            note="用户消息先写入数据库，后面的滑动窗口会排除这条当前消息，避免重复塞进 prompt。",
        )

        recent_text = await _build_recent_memory_text(
            db,
            conv,
            current_message_id=user_message.id,
            trace_id=trace.trace_id,
        )
        memory_context = _build_memory_context(
            db,
            conv,
            current_message_id=user_message.id,
            recent_text=recent_text,
        )
        retrieval_question = _build_memory_aware_retrieval_question(effective_question, memory_context)
        trace.add(
            "memory_built",
            "_build_memory_context",
            uses={
                "conversation_id": cid,
                "memory_summary": conv.memory_summary or "",
                "summary_upto_message_id": conv.memory_summary_upto_message_id or 0,
            },
            creates={
                "recent_text": recent_text,
                "memory_context": memory_context,
                "retrieval_question": retrieval_question,
            },
            result={
                "memory_used": bool(memory_context),
                "used_for_retrieval": retrieval_question != effective_question,
                "window_turns": MEMORY_WINDOW_TURNS,
            },
            note="系统构造短期滑动窗口和长期摘要记忆，并生成真正用于 RAG 检索的问题。",
        )

        rag_gate = await decide_need_rag(
            effective_question,
            memory_context,
            knowledge_base.name,
            body.attachments,
        )
        need_rag = bool(rag_gate.get("need_rag", True))
        generation_mode = "rag" if need_rag else "direct"
        trace.add(
            "rag_gate_decided",
            "decide_need_rag",
            uses={
                "effective_question": effective_question,
                "memory_context": memory_context,
                "knowledge_base_name": knowledge_base.name,
                "attachments_count": len(body.attachments or []),
            },
            creates={"rag_gate": rag_gate},
            result={
                "need_rag": need_rag,
                "route": rag_gate.get("route", generation_mode),
                "confidence": rag_gate.get("confidence", 0),
                "source": rag_gate.get("source", ""),
                "reason": rag_gate.get("reason", ""),
            },
            note="模型先判断这轮问题是否需要进入知识库检索。若判定为直答，则跳过 RAG，只用问题和会话记忆生成回答。",
        )

        context = ""
        sources = []
        retrieved_contexts = []
        knowledge_chunks = []
        retrieval_trace = {
            "embedding": embedding_backend_status(),
            "query_plan": {},
            "routes": [],
            "rrf": [],
            "rerank": {"status": "skipped", "items": []},
            "memory": {
                "used": bool(memory_context),
                "used_for_retrieval": False,
                "window_turns": MEMORY_WINDOW_TURNS,
                "summary_available": bool(conv.memory_summary),
                "summary_upto_message_id": conv.memory_summary_upto_message_id or 0,
            },
            "rag_gate": rag_gate,
            "mode": generation_mode,
            "effective_question": effective_question,
            "retrieval_question": retrieval_question,
        }
        if body.attachments:
            retrieval_trace["image_analysis_status"] = image_analysis.get("status", "")
            retrieval_trace["image_analysis_error"] = image_analysis.get("error", "")
            retrieval_trace["image_description"] = image_analysis.get("description", "")

        if need_rag:
            knowledge_chunks, retrieval_trace = await agentic_retrieve_knowledge(
                retrieval_question,
                knowledge_base_id=knowledge_base.id,
                db=db,
                rag_gate=rag_gate,
                memory_context=memory_context,
                trace_recorder=trace,
            )
            retrieval_trace = retrieval_trace or {}
            retrieval_trace["memory"] = {
                "used": bool(memory_context),
                "used_for_retrieval": retrieval_question != effective_question,
                "window_turns": MEMORY_WINDOW_TURNS,
                "summary_available": bool(conv.memory_summary),
                "summary_upto_message_id": conv.memory_summary_upto_message_id or 0,
            }
            retrieval_trace["rag_gate"] = rag_gate
            retrieval_trace["mode"] = generation_mode
            retrieval_trace["effective_question"] = effective_question
            retrieval_trace["retrieval_question"] = retrieval_question
            if body.attachments:
                retrieval_trace["image_analysis_status"] = image_analysis.get("status", "")
                retrieval_trace["image_analysis_error"] = image_analysis.get("error", "")
                retrieval_trace["image_description"] = image_analysis.get("description", "")
            trace.add(
                "retrieval_completed",
                "langchain_agentic_retrieve_knowledge",
                params={"question": retrieval_question, "knowledge_base_id": knowledge_base.id},
                creates={
                    "query_plan": retrieval_trace.get("query_plan", {}),
                    "routes": retrieval_trace.get("routes", []),
                    "rrf": retrieval_trace.get("rrf", []),
                    "rerank": retrieval_trace.get("rerank", {}),
                    "agent": retrieval_trace.get("agent", {}),
                },
                result={"final_chunks_count": len(knowledge_chunks)},
                note="LangChain Agent retrieval completed with bounded planning, tool calls, and selected context.",
            )
            if knowledge_chunks:
                context = "\n\n".join(
                    f"[来源: {c['file_name']}]\n{c['content']}"
                    for c in knowledge_chunks
                )
            sources = _build_sources(knowledge_chunks)
            retrieved_contexts = [c.get("content", "") for c in knowledge_chunks if c.get("content")]
            trace.add(
                "context_built",
                "_build_sources",
                creates={"context": context, "sources": sources},
                result={"sources_count": len(sources), "retrieved_contexts_count": len(retrieved_contexts)},
                note="系统把最终选中的 chunk 拼成给大模型看的知识库上下文，并生成前端可展开的参考资料。",
            )
        else:
            retrieval_trace["skip_reason"] = rag_gate.get("reason", "")
            trace.add(
                "retrieval_skipped",
                "decide_need_rag",
                uses={
                    "effective_question": effective_question,
                    "memory_context": memory_context,
                },
                result={
                    "need_rag": False,
                    "route": "direct",
                    "confidence": rag_gate.get("confidence", 0),
                    "reason": rag_gate.get("reason", ""),
                },
                note="本轮判定为直答，不进入知识库检索，也不启动 RAGAS。回答将只结合问题与会话记忆。",
            )

        async def event_stream():
            full = ""
            failed = False
            first_chunk_seen = False
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
                for payload in _trace_sse_payloads(trace):
                    yield payload
                yield f"data: {analysis_data}\n\n"
            for payload in _trace_sse_payloads(trace):
                yield payload
            yield f"data: {conversation_data}\n\n"
            if need_rag and sources:
                data = json.dumps({"type": "sources", "sources": sources}, ensure_ascii=False)
                yield f"data: {data}\n\n"
            trace.add(
                "generation_started",
                "stream_rag_answer",
                params={
                    "question": effective_question,
                    "memory_context": memory_context,
                    "context": context,
                    "mode": generation_mode,
                },
                note=(
                    "开始调用文本模型。"
                    if need_rag
                    else "开始调用文本模型。当前问题被判定为直答，不进入知识库检索，只结合问题与会话记忆回答。"
                ),
            )
            for payload in _trace_sse_payloads(trace):
                yield payload
            async for event in stream_rag_answer(
                effective_question,
                context,
                memory_context,
                trace,
                use_rag=need_rag,
            ):
                for payload in _trace_sse_payloads(trace):
                    yield payload
                if isinstance(event, dict):
                    if event.get("type") == "error":
                        failed = True
                        trace.add(
                            "generation_failed",
                            "_stream_deepseek_response",
                            result={"message": event.get("message") or event.get("content") or "DeepSeek 网络请求失败"},
                            note="模型生成阶段失败，系统会返回错误事件，并且不会保存失败 assistant 消息。",
                        )
                        for payload in _trace_sse_payloads(trace):
                            yield payload
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
                    if chunk and not first_chunk_seen:
                        first_chunk_seen = True
                        trace.add(
                            "first_content_chunk",
                            "_stream_openai_chat_chunks",
                            result={"chunk": chunk},
                            note="大模型开始返回第一段流式内容，前端会逐步拼接为正在生成的回答。",
                        )
                        for payload in _trace_sse_payloads(trace):
                            yield payload
                    full += chunk

            # save assistant message
            if full and not failed:
                _safe_trace_add(
                    trace,
                    "assistant_ready_to_save",
                    "Message",
                    uses={
                        "full_answer": full,
                        "sources_count": len(sources),
                        "need_rag": need_rag,
                    },
                    note=(
                        "模型完整回答成功，系统准备保存 assistant 消息，并启动摘要判断。"
                        if not need_rag
                        else "模型完整回答成功，系统准备保存 assistant 消息，并启动 RAGAS 和摘要判断。"
                    ),
                )
                retrieval_trace["learning_trace"] = compact_trace_reference(trace.snapshot())
                assistant_message = None
                try:
                    assistant_message = Message(
                        conversation_id=cid,
                        role="assistant",
                        content=full,
                        sources=json.dumps(sources, ensure_ascii=False),
                        ragas_status="pending" if need_rag else "",
                        retrieval_trace=json.dumps(retrieval_trace, ensure_ascii=False),
                    )
                    db.add(assistant_message)
                    db.commit()
                    db.refresh(assistant_message)
                except Exception as exc:
                    db.rollback()
                    logger.warning("Assistant message save failed after stream finished: %s", exc, exc_info=True)
                    _safe_trace_add(
                        trace,
                        "assistant_save_failed",
                        "Message",
                        result={"error": str(exc)},
                        note="模型回答已经生成完毕，但保存 assistant 消息失败。系统仍会结束流，避免前端误报 network error。",
                    )

                if assistant_message:
                    _safe_trace_attach(trace, conversation_id=cid, message_id=assistant_message.id)
                    _safe_trace_add(
                        trace,
                        "assistant_message_saved",
                        "Message",
                        creates={"assistant_message_id": assistant_message.id},
                        result={"ragas_status": "pending" if need_rag else ""},
                        note="assistant 消息保存成功，历史会话刷新后仍可从这条消息打开流程。",
                    )
                    if need_rag:
                        try:
                            schedule_ragas_evaluation(
                                assistant_message.id,
                                effective_question,
                                full,
                                retrieved_contexts,
                                trace.trace_id,
                            )
                            _safe_trace_add(
                                trace,
                                "ragas_scheduled",
                                "schedule_ragas_evaluation",
                                params={
                                    "message_id": assistant_message.id,
                                    "question": effective_question,
                                    "answer_chars": len(full),
                                    "contexts_count": len(retrieved_contexts),
                                },
                                note="RAGAS 在 assistant 保存后异步启动，不阻塞用户看到答案。",
                            )
                        except Exception as exc:
                            logger.warning("RAGAS schedule failed after stream finished: %s", exc, exc_info=True)
                            _safe_trace_add(
                                trace,
                                "ragas_schedule_failed",
                                "schedule_ragas_evaluation",
                                result={"error": str(exc)},
                                note="RAGAS 调度失败，但不影响主回答完成。",
                            )
                    else:
                        _safe_trace_add(
                            trace,
                            "ragas_skipped",
                            "schedule_ragas_evaluation",
                            result={"need_rag": False, "reason": rag_gate.get("reason", "")},
                            note="当前问题被路由为直答，因此不启动 RAGAS。",
                        )
                    try:
                        _schedule_memory_summary_update(cid, trace.trace_id)
                        _safe_trace_add(
                            trace,
                            "memory_summary_update_scheduled",
                            "_schedule_memory_summary_update",
                            params={"conversation_id": cid},
                            note="系统异步检查长期记忆是否超过上限，若超过则进行二次摘要。",
                        )
                    except Exception as exc:
                        logger.warning("Memory summary schedule failed after stream finished: %s", exc, exc_info=True)
                        _safe_trace_add(
                            trace,
                            "memory_summary_update_schedule_failed",
                            "_schedule_memory_summary_update",
                            result={"error": str(exc)},
                            note="长期记忆压缩调度失败，但不影响主回答完成。",
                        )
                    try:
                        retrieval_trace["learning_trace"] = compact_trace_reference(trace.snapshot())
                        assistant_message.retrieval_trace = json.dumps(retrieval_trace, ensure_ascii=False)
                        db.commit()
                    except Exception as exc:
                        db.rollback()
                        logger.warning("Assistant trace reference update failed: %s", exc, exc_info=True)
                _safe_trace_finish(
                    trace,
                    "done" if assistant_message else "partial",
                    conversation_id=cid,
                    message_id=assistant_message.id if assistant_message else None,
                )
                for payload in _trace_sse_payloads(trace):
                    yield payload
            elif failed:
                _safe_trace_add(
                    trace,
                    "assistant_not_saved",
                    "Message",
                    result={"saved": False},
                    note="回答生成失败，遵循项目规则：不把失败内容保存为正式 assistant 消息。",
                )
                _safe_trace_finish(trace, "failed", conversation_id=cid)
                for payload in _trace_sse_payloads(trace):
                    yield payload
            db.close()
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")
    except Exception:
        db.close()
        raise
