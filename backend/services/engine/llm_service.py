
import json
import logging
from typing import Optional

import httpx

from config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_MODEL,
    TEXT_FALLBACK_API_KEY,
    TEXT_FALLBACK_BASE_URL,
    TEXT_FALLBACK_ENABLED,
    TEXT_FALLBACK_MODEL,
)
from learning_trace import TraceRecorder
from tool import deepseek_chat_url, normalize_deepseek_model


logger = logging.getLogger(__name__)


def _openai_chat_url(base_url: str) -> str:
    base_url = (base_url or "").rstrip("/")
    if base_url.endswith("/v1"):
        return f"{base_url}/chat/completions"
    return f"{base_url}/v1/chat/completions"


async def _stream_deepseek_response(
    question: str,
    context: str,
    memory_context: str = "",
    trace: TraceRecorder | None = None,
    use_rag: bool = True,
):
    if not DEEPSEEK_API_KEY:
        if trace:
            trace.add(
                "deepseek_missing_config",
                "_stream_deepseek_response",
                result={"configured": False},
                note="DeepSeek API Key 未配置，文本生成无法开始。",
            )
        yield {"type": "error", "message": _model_missing_error(False)}
        return

    if use_rag:
        system_prompt = (
            "你是企业知识库智能问答助手。请优先基于提供的企业知识库上下文回答用户问题；"
            "如果知识库信息不足，请明确说明不足之处，并给出可验证的建议。"
            "回答使用中文，结构清晰，避免编造未在上下文中出现的事实。"
            "信息优先级为：企业知识库上下文 > 当前用户问题 > 会话记忆。"
            "会话记忆只能用于理解指代、延续任务和用户偏好，不能替代知识库事实依据。"
        )
        user_prompt = (
            f"用户问题：{question}\n\n"
            f"会话记忆：\n{memory_context or '（无）'}\n\n"
            f"企业知识库上下文：\n{context or '（未检索到相关知识库内容）'}"
        )
    else:
        system_prompt = (
            "你是企业对话助手。当前问题不需要知识库检索，请直接回答。"
            "你可以结合会话记忆理解指代和上下文，但不要声称自己检索到了知识库。"
            "如果是数学、常识、闲聊、会话统计或普通说明题，请直接给出简洁准确的中文回答。"
            "如果信息不足，请明确说明，而不是编造知识库事实。"
        )
        user_prompt = (
            f"用户问题：{question}\n\n"
            f"会话记忆：\n{memory_context or '（无）'}\n\n"
            "回答要求：直接回答当前问题，不要引用知识库上下文，也不要提到检索过程。"
        )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    if trace:
        trace.add(
            "generation_prompt_built",
            "_stream_deepseek_response",
            creates={
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "mode": "rag" if use_rag else "direct",
            },
            note=(
                "系统构造最终给文本模型的 messages。注意：会话记忆和知识库上下文是分区放入 prompt 的。"
                if use_rag
                else "系统构造最终给文本模型的直答 messages。会话记忆仅辅助理解指代，不会进入知识库上下文。"
            ),
        )

    async for event in _stream_openai_chat_chunks(
        deepseek_chat_url(),
        DEEPSEEK_API_KEY,
        normalize_deepseek_model(DEEPSEEK_MODEL),
        messages,
        "DeepSeek",
    ):
        if isinstance(event, dict) and event.get("type") == "error":
            if trace:
                trace.add(
                    "deepseek_failed",
                    "_stream_openai_chat_chunks",
                    result={
                        "reason": event.get("reason"),
                        "status_code": event.get("status_code"),
                        "detail": event.get("detail", ""),
                    },
                    note="DeepSeek 流式调用失败。若属于网络或 5xx 错误，系统会尝试文本后备模型。",
                )
            if _should_use_text_fallback(event):
                logger.warning(
                    "DeepSeek failed with retryable error, switching to text fallback: reason=%s status=%s",
                    event.get("reason"),
                    event.get("status_code"),
                )
                if trace:
                    trace.add(
                        "text_fallback_started",
                        "_stream_text_fallback_response",
                        params={"model": TEXT_FALLBACK_MODEL, "enabled": TEXT_FALLBACK_ENABLED},
                        note="DeepSeek 暂不可用，系统尝试使用兼容文本后备模型继续回答。",
                    )
                async for fallback_event in _stream_text_fallback_response(messages, trace):
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


async def _stream_text_fallback_response(messages: list[dict], trace: TraceRecorder | None = None):
    if not TEXT_FALLBACK_ENABLED:
        if trace:
            trace.add(
                "text_fallback_disabled",
                "_stream_text_fallback_response",
                result={"enabled": False},
                note="文本后备模型未启用，因此 DeepSeek 失败后无法继续生成。",
            )
        yield {
            "type": "error",
            "message": "DeepSeek 网络请求失败，且文本后备模型未启用，请设置 TEXT_FALLBACK_ENABLED=true。",
        }
        return
    if not TEXT_FALLBACK_API_KEY:
        if trace:
            trace.add(
                "text_fallback_missing_config",
                "_stream_text_fallback_response",
                result={"configured": False},
                note="文本后备模型缺少 API Key，无法接管 DeepSeek 的失败请求。",
            )
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
            if trace:
                trace.add(
                    "text_fallback_failed",
                    "_stream_openai_chat_chunks",
                    result={
                        "reason": event.get("reason"),
                        "status_code": event.get("status_code"),
                        "detail": event.get("detail", ""),
                    },
                    note="文本后备模型也调用失败，本轮回答会以错误结束。",
                )
            yield {
                "type": "error",
                "message": _text_fallback_error_message(
                    event.get("reason") or "api",
                    event.get("status_code"),
                ),
            }
            return
        if trace:
            trace.add(
                "text_fallback_chunk",
                "_stream_openai_chat_chunks",
                result={"chunk": event},
                note="文本后备模型开始返回内容，前端会继续按同一条流式回答展示。",
            )
            trace = None
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
