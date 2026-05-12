from rag.learning_trace import TraceRecorder
from rag.llm import stream_answer_events


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
    """Compatibility wrapper; the real generation chain lives in services.langchain_rag."""
    async for event in stream_answer_events(question, context, memory_context, trace, use_rag):
        yield event
