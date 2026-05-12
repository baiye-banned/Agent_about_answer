import json
import logging
import re
from typing import Any

from langchain_openai import ChatOpenAI

from config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    TEXT_FALLBACK_API_KEY,
    TEXT_FALLBACK_BASE_URL,
    TEXT_FALLBACK_ENABLED,
    TEXT_FALLBACK_MODEL,
)


logger = logging.getLogger(__name__)


def normalize_deepseek_model(model: str) -> str:
    aliases = {
        "deepseekv4flash": "deepseek-v4-flash",
        "deepseekv4pro": "deepseek-v4-pro",
    }
    return aliases.get((model or "").lower(), model)


def _openai_base_url(base_url: str) -> str:
    base_url = (base_url or "").rstrip("/")
    if base_url.endswith("/chat/completions"):
        base_url = base_url[: -len("/chat/completions")]
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"
    return base_url


def deepseek_chat_url() -> str:
    return f"{_openai_base_url(DEEPSEEK_BASE_URL)}/chat/completions"


def get_deepseek_model(*, streaming: bool = False, temperature: float = 0.1, max_tokens: int | None = None):
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DeepSeek API Key 未配置")
    kwargs = {
        "api_key": DEEPSEEK_API_KEY,
        "base_url": _openai_base_url(DEEPSEEK_BASE_URL),
        "model": normalize_deepseek_model(DEEPSEEK_MODEL),
        "temperature": temperature,
        "streaming": streaming,
        "timeout": 60,
        "max_retries": 0,
    }
    if max_tokens is not None:
        kwargs["max_completion_tokens"] = max_tokens
    return ChatOpenAI(**kwargs)


def get_text_fallback_model(*, streaming: bool = False, temperature: float = 0.1, max_tokens: int | None = None):
    if not TEXT_FALLBACK_API_KEY:
        raise RuntimeError("文本后备模型 API Key 未配置")
    kwargs = {
        "api_key": TEXT_FALLBACK_API_KEY,
        "base_url": _openai_base_url(TEXT_FALLBACK_BASE_URL),
        "model": TEXT_FALLBACK_MODEL,
        "temperature": temperature,
        "streaming": streaming,
        "timeout": 60,
        "max_retries": 0,
    }
    if max_tokens is not None:
        kwargs["max_completion_tokens"] = max_tokens
    return ChatOpenAI(**kwargs)


async def call_chat_text(system_prompt: str, user_prompt: str, *, max_tokens: int = 900) -> str:
    model = get_deepseek_model(streaming=False, temperature=0.1, max_tokens=max_tokens)
    message = await model.ainvoke([
        ("system", system_prompt),
        ("user", user_prompt),
    ])
    return _message_text(message.content)


async def call_chat_json(system_prompt: str, user_prompt: str, *, max_tokens: int = 800) -> dict:
    content = await call_chat_text(system_prompt, user_prompt, max_tokens=max_tokens)
    return parse_json_object(content)


async def call_router_json(payload: dict) -> dict:
    if not TEXT_FALLBACK_API_KEY:
        raise RuntimeError("路由模型 API Key 未配置")
    model = get_text_fallback_model(streaming=False, temperature=0, max_tokens=300)
    system_prompt = (
        "你是企业知识库 RAG 路由器，只判断当前问题是否需要检索企业知识库。"
        "只输出 JSON，不要输出 Markdown。字段必须是："
        "need_rag(boolean), route(string: rag/direct), confidence(number:0-1), reason(string), source(string)。"
        "如果问题询问公司制度、规章、流程、文件、知识库或上传资料内容，need_rag=true。"
        "如果只是数学、常识、闲聊、翻译、润色、代码解释或当前会话统计，need_rag=false。"
        "图片附件已经被转成文本；如果只需根据图片描述回答，不需要知识库，need_rag=false。"
        "只要不确定，need_rag=true。"
    )
    message = await model.ainvoke([
        ("system", system_prompt),
        ("user", json.dumps(payload, ensure_ascii=False)),
    ])
    return parse_json_object(_message_text(message.content))


async def stream_answer_events(
    question: str,
    context: str,
    memory_context: str = "",
    trace: Any = None,
    use_rag: bool = True,
):
    messages = build_answer_messages(question, context, memory_context, use_rag)
    _trace_add(
        trace,
        "langchain_generation_prompt_built",
        "LangChain ChatOpenAI",
        creates={
            "system_prompt": messages[0][1],
            "user_prompt": messages[1][1],
            "mode": "rag" if use_rag else "direct",
        },
        note="LangChain 生成链构造最终 messages，记忆和知识库上下文分区进入 prompt。",
    )

    try:
        async for chunk in _stream_model_chunks(get_deepseek_model(streaming=True, temperature=0.1), messages):
            yield chunk
        return
    except Exception as exc:
        _trace_add(
            trace,
            "langchain_generation_failed",
            "ChatOpenAI.astream",
            result={"provider": "DeepSeek", "error": str(exc)},
            note="DeepSeek 流式生成失败；如果文本后备模型可用，LangChain 会切换到后备模型继续生成。",
        )
        if not TEXT_FALLBACK_ENABLED:
            yield {
                "type": "error",
                "message": "DeepSeek 网络请求失败，且文本后备模型未启用，请设置 TEXT_FALLBACK_ENABLED=true。",
            }
            return
        if not TEXT_FALLBACK_API_KEY:
            yield {
                "type": "error",
                "message": "DeepSeek 网络请求失败，且文本后备模型未配置，请配置 TEXT_FALLBACK_API_KEY 或 DASHSCOPE_API_KEY。",
            }
            return

    try:
        _trace_add(
            trace,
            "langchain_text_fallback_started",
            "ChatOpenAI.astream",
            params={"model": TEXT_FALLBACK_MODEL},
            note="LangChain 切换到 OpenAI 兼容文本后备模型继续本轮回答。",
        )
        async for chunk in _stream_model_chunks(get_text_fallback_model(streaming=True, temperature=0.1), messages):
            yield chunk
    except Exception as exc:
        _trace_add(
            trace,
            "langchain_text_fallback_failed",
            "ChatOpenAI.astream",
            result={"error": str(exc)},
            note="文本后备模型也生成失败，本轮回答返回错误事件。",
        )
        yield {
            "type": "error",
            "message": f"回答生成失败：{exc}",
        }


def build_answer_messages(question: str, context: str, memory_context: str, use_rag: bool) -> list[tuple[str, str]]:
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
            "如果信息不足，请明确说明，而不是编造知识库事实。"
        )
        user_prompt = (
            f"用户问题：{question}\n\n"
            f"会话记忆：\n{memory_context or '（无）'}\n\n"
            "回答要求：直接回答当前问题，不要引用知识库上下文，也不要提到检索过程。"
        )
    return [("system", system_prompt), ("user", user_prompt)]


async def _stream_model_chunks(model: ChatOpenAI, messages: list[tuple[str, str]]):
    async for chunk in model.astream(messages):
        text = _message_text(chunk.content)
        if text:
            yield text


def parse_json_object(content: str) -> dict:
    content = (content or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?", "", content).strip()
        content = re.sub(r"```$", "", content).strip()
    match = re.search(r"\{.*\}", content, flags=re.S)
    if match:
        content = match.group(0)
    return json.loads(content)


def _message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item or ""))
        return "".join(parts)
    return str(content or "")


def _trace_add(trace: Any, *args, **kwargs) -> None:
    if not trace:
        return
    try:
        trace.add(*args, **kwargs)
    except Exception:
        logger.debug("Trace add failed", exc_info=True)
