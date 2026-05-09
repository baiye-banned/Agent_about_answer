import asyncio
import concurrent.futures
import json
import logging
import time

from config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    EMBEDDING_API_KEY,
    EMBEDDING_BASE_URL,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    RAGAS_ENABLED,
    RAGAS_LLM_MODEL,
    RAGAS_MAX_ANSWER_CHARS,
    RAGAS_MAX_CONTEXT_CHARS,
    RAGAS_MAX_CONTEXTS,
    RAGAS_METRIC_TIMEOUT_SECONDS,
    RAGAS_TIMEOUT_SECONDS,
)
from database import SessionLocal
from models import Message
from retrieval import normalize_deepseek_model


logger = logging.getLogger(__name__)


async def evaluate_message_async(message_id: int, question: str, answer: str, contexts: list[str]):
    if not RAGAS_ENABLED:
        _mark_message(message_id, "failed", {}, "RAGAS 已关闭")
        return

    logger.info("RAGAS evaluation started: message_id=%s contexts=%s", message_id, len(contexts or []))
    _mark_message(message_id, "running", {}, "")
    try:
        prepared_answer = _truncate_text(answer, RAGAS_MAX_ANSWER_CHARS)
        prepared_contexts = _prepare_contexts(contexts)
        scores = await asyncio.wait_for(
            asyncio.to_thread(_evaluate_message_sync, question, prepared_answer, prepared_contexts),
            timeout=RAGAS_TIMEOUT_SECONDS,
        )
        errors = scores.pop("_errors", {})
        if scores:
            error_text = _format_metric_errors(errors)
            _mark_message(message_id, "done", scores, error_text)
            logger.info("RAGAS evaluation done: message_id=%s scores=%s errors=%s", message_id, scores, errors)
        else:
            error_text = _format_metric_errors(errors) or "RAGAS 评测失败：所有指标均未返回结果"
            _mark_message(message_id, "failed", {}, error_text)
            logger.warning("RAGAS evaluation failed: message_id=%s errors=%s", message_id, errors)
    except asyncio.TimeoutError:
        message = f"RAGAS 评测超时：超过 {RAGAS_TIMEOUT_SECONDS} 秒未完成，请稍后重试"
        logger.warning("RAGAS evaluation timeout: message_id=%s timeout=%s", message_id, RAGAS_TIMEOUT_SECONDS)
        _mark_message(message_id, "failed", {}, message)
    except Exception as exc:
        logger.warning("RAGAS evaluation failed: message_id=%s error=%s", message_id, exc, exc_info=True)
        _mark_message(message_id, "failed", {}, f"RAGAS 评测失败：{_friendly_error(exc)}")


def schedule_ragas_evaluation(message_id: int, question: str, answer: str, contexts: list[str]):
    try:
        asyncio.create_task(evaluate_message_async(message_id, question, answer, contexts))
    except RuntimeError:
        _mark_message(message_id, "failed", {}, "当前事件循环不可用，无法启动 RAGAS 评测")


def _evaluate_message_sync(question: str, answer: str, contexts: list[str]) -> dict:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DeepSeek API Key 未配置")
    if not EMBEDDING_BASE_URL or not EMBEDDING_API_KEY:
        raise RuntimeError("Embedding API 未配置")

    from openai import OpenAI
    from ragas.dataset_schema import SingleTurnSample
    from ragas.embeddings import OpenAIEmbeddings
    from ragas.llms import llm_factory
    from ragas.metrics import Faithfulness, LLMContextPrecisionWithoutReference, ResponseRelevancy

    embedding_client = OpenAI(
        api_key=EMBEDDING_API_KEY,
        base_url=_openai_base_url(EMBEDDING_BASE_URL),
    )
    embeddings = _ResponseRelevancyEmbeddingsAdapter(
        OpenAIEmbeddings(client=embedding_client, model=EMBEDDING_MODEL)
    )

    llm_client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=_openai_base_url(DEEPSEEK_BASE_URL),
    )
    llm = _make_ragas_llm(llm_factory, llm_client)
    sample = SingleTurnSample(
        user_input=question,
        response=answer,
        retrieved_contexts=contexts or [],
    )

    metrics = {
        "faithfulness": Faithfulness(llm=llm),
        "context_precision_without_reference": LLMContextPrecisionWithoutReference(llm=llm),
        "response_relevancy": ResponseRelevancy(llm=llm, embeddings=embeddings),
    }

    scores = {}
    errors = {}
    for key, metric in metrics.items():
        started_at = time.monotonic()
        try:
            scores[key] = _score_metric_with_timeout(metric, sample, RAGAS_METRIC_TIMEOUT_SECONDS)
            logger.info("RAGAS metric done: metric=%s seconds=%.2f", key, time.monotonic() - started_at)
        except Exception as exc:
            errors[key] = _friendly_error(exc)
            logger.warning(
                "RAGAS metric failed: metric=%s seconds=%.2f error=%s",
                key,
                time.monotonic() - started_at,
                exc,
                exc_info=True,
            )

    if errors:
        scores["_errors"] = errors
    return scores


def _make_ragas_llm(llm_factory, client):
    model = normalize_deepseek_model(RAGAS_LLM_MODEL)
    return llm_factory(model=model, client=client, max_tokens=4096, temperature=0)


class _ResponseRelevancyEmbeddingsAdapter:
    """Expose the LangChain-style methods expected by RAGAS 0.4.2."""

    def __init__(self, embeddings):
        self.embeddings = embeddings

    def embed_query(self, text: str):
        return self.embeddings.embed_text(text, dimensions=EMBEDDING_DIM)

    def embed_documents(self, texts: list[str]):
        return self.embeddings.embed_texts(texts, dimensions=EMBEDDING_DIM)


def _score_metric(metric, sample):
    score = metric.single_turn_score(sample)
    return round(float(score), 4)


def _score_metric_with_timeout(metric, sample, timeout_seconds: int):
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_score_metric, metric, sample)
    try:
        return future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"metric timeout after {timeout_seconds}s") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _mark_message(message_id: int, status: str, scores: dict, error: str):
    db = SessionLocal()
    try:
        message = db.query(Message).filter_by(id=message_id).first()
        if not message:
            return
        message.ragas_status = status
        message.ragas_scores = json.dumps(scores, ensure_ascii=False) if scores else ""
        message.ragas_error = error or ""
        db.commit()
    finally:
        db.close()


def _format_metric_errors(errors: dict) -> str:
    if not errors:
        return ""
    details = "；".join(f"{name}: {message}" for name, message in errors.items())
    return f"部分 RAGAS 指标评测失败：{details}"


def _friendly_error(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    lowered = text.lower()
    if "max_tokens" in text or "length limit" in lowered or "incomplete" in lowered:
        return "DeepSeek 结构化输出被截断，请调高 RAGAS evaluator max_tokens 或缩短回答/上下文"
    if "timed out" in lowered or "timeout" in lowered:
        return "RAGAS 评测超时"
    if "embedding" in lowered:
        return f"Embedding 调用失败：{text}"
    if "deepseek" in lowered or "connection" in lowered or "connect" in lowered:
        return f"DeepSeek 调用失败：{text}"
    return text


def _openai_base_url(base_url: str) -> str:
    base_url = (base_url or "").rstrip("/")
    if base_url.endswith("/v1"):
        return base_url
    return f"{base_url}/v1"


def _prepare_contexts(contexts: list[str]) -> list[str]:
    prepared = []
    for item in contexts or []:
        text = _truncate_text(item, RAGAS_MAX_CONTEXT_CHARS)
        if text:
            prepared.append(text)
        if len(prepared) >= RAGAS_MAX_CONTEXTS:
            break
    return prepared


def _truncate_text(text: str, max_chars: int) -> str:
    value = (text or "").strip()
    if not value or max_chars <= 0:
        return ""
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + "\n...(已截断)"
