from rag.llm import stream_answer_events


async def stream_rag_answer(
    question: str,
    context: str,
    memory_context: str = "",
    trace=None,
    use_rag: bool = True,
):
    async for event in stream_answer_events(question, context, memory_context, trace, use_rag):
        yield event
