
import logging
from typing import Optional

import httpx

from config import VISION_API_KEY, VISION_BASE_URL, VISION_MODEL
from services.base.oss_service import _public_oss_url
from services.engine.llm_service import _openai_chat_url


logger = logging.getLogger(__name__)


async def _build_effective_question(
    question: str,
    attachments: Optional[list[dict]] = None,
) -> tuple[str, dict]:
    attachments = attachments or []
    question = (question or "").strip()
    if not attachments:
        return question, {}

    image_analysis = await _analyze_image_attachments(attachments, question)
    description = image_analysis.get("description", "").strip()

    if question and description:
        return f"{question}\n\n图片内容：{description}", image_analysis
    if description:
        return description, image_analysis
    return question, image_analysis


async def _analyze_image_attachments(attachments: list[dict], question: str = "") -> dict:
    image_urls = _build_image_urls(attachments)
    if not image_urls:
        return {
            "status": "failed",
            "description": "",
            "error": "图片附件缺少可访问的 OSS object_key，请重新上传后再试。",
        }
    if not VISION_API_KEY:
        return {
            "status": "failed",
            "description": "",
            "error": "图片内容提取失败，请检查 VISION_MODEL/VISION_API_KEY/OSS URL。",
        }

    prompts = _image_analysis_prompts(question)
    last_error = ""
    last_description = ""
    for prompt in prompts:
        try:
            description = await _request_image_description(prompt, image_urls)
        except Exception as exc:
            last_error = str(exc)
            continue

        description = (description or "").strip()
        if not description:
            last_error = "图片内容提取失败：视觉模型未返回图片描述。"
            continue

        status, error = _classify_image_analysis(description)
        if status == "failed":
            last_error = error or "图片内容识别失败，请检查清晰度后重新上传。"
            last_description = description
            continue

        return {
            "status": status,
            "description": description,
            "error": error,
        }

    if last_description:
        status, error = _classify_image_analysis(last_description)
        if status in {"partial", "success"}:
            return {
                "status": status,
                "description": last_description,
                "error": error,
            }

    return {
        "status": "failed",
        "description": "",
        "error": last_error or "图片内容识别失败，请检查清晰度后重新上传。",
    }


def _image_analysis_prompts(question: str = "") -> list[str]:
    question = (question or "").strip()
    if question:
        return [
            (
                "请先逐字识别图片中可见的文字、题目、选项、表格、数字和图表标签，"
                "再用中文概括图片内容，并明确说明哪些内容看清了，哪些内容不够清晰。"
                "不要编造图片中不存在的信息。"
                f"\n用户问题：{question}\n请重点关注与该问题相关的图片信息。"
            ),
            (
                "请再次仔细查看这张图片，优先识别可见文字、题目、选项、数字和表格。"
                "如果局部模糊，也要尽量保留可辨认部分，并说明无法确认的区域。"
                "不要编造图片中不存在的信息。"
                f"\n用户问题：{question}"
            ),
        ]
    return [
        (
            "请尽可能逐字识别这张图片中的所有可见文字、题目、选项、数字、表格和图表标签，"
            "先抄录能看清的内容，再总结图片整体含义。"
            "如果有模糊或看不清的部分，请明确指出，不要编造。"
        ),
        (
            "请重新检查这张图片，专注于文字识别和局部细节。"
            "请输出可辨认的原文、题目、选项、数字和表格内容，并说明哪些地方无法识别。"
        ),
    ]


async def _request_image_description(prompt: str, image_urls: list[str]) -> str:
    user_content = [{"type": "text", "text": prompt}]
    user_content.extend({"type": "image_url", "image_url": {"url": url}} for url in image_urls)
    payload = {
        "model": VISION_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": "你是图片内容理解助手，负责将图片转写为适合检索和问答的中文文本。"},
            {"role": "user", "content": user_content},
        ],
    }
    headers = {
        "Authorization": f"Bearer {VISION_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(_openai_chat_url(VISION_BASE_URL), json=payload, headers=headers)
        if response.status_code >= 400:
            detail = response.text[:300]
            logger.warning("Vision description failed: status=%s detail=%s", response.status_code, detail)
            raise RuntimeError("图片内容提取失败，请检查 VISION_MODEL/VISION_API_KEY/OSS URL。")
        description = response.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        return (description or "").strip()
    except httpx.HTTPError as exc:
        logger.warning("Vision description network request failed: %s", exc, exc_info=True)
        raise RuntimeError("图片内容提取失败，请检查 VISION_MODEL/VISION_API_KEY/OSS URL。") from exc


def _classify_image_analysis(description: str) -> tuple[str, str]:
    text = (description or "").strip()
    if not text:
        return "failed", "图片内容提取失败：视觉模型未返回图片描述。"

    failure_phrases = [
        "看不清",
        "无法识别",
        "无法看清",
        "无法辨认",
        "无法读取",
        "过于模糊",
        "图片模糊",
        "不清晰",
        "无法确定",
        "难以识别",
        "图片质量",
        "无法完全识别",
    ]
    partial_phrases = [
        "部分",
        "大致",
        "可能",
        "疑似",
        "不够清晰",
        "部分可见",
        "仅能看到",
        "只能识别",
    ]
    useful_markers = [
        "可见",
        "看到",
        "能看到",
        "识别到",
        "文字为",
        "内容为",
        "显示",
        "题目",
        "选项",
        "数字",
        "表格",
        "图表",
    ]

    if any(phrase in text for phrase in failure_phrases):
        has_partial_signal = any(phrase in text for phrase in partial_phrases)
        has_useful_content = any(marker in text for marker in useful_markers)
        if has_partial_signal and has_useful_content:
            return "partial", "图片内容仅部分识别，请结合文字问题查看。"
        return "failed", "图片内容未能清晰识别，请检查图片清晰度后重试。"

    if any(phrase in text for phrase in partial_phrases):
        return "partial", "图片内容仅部分识别，请结合文字问题查看。"

    return "success", ""


def _build_image_urls(attachments: list[dict]) -> list[str]:
    urls = []
    for item in attachments:
        object_key = item.get("object_key")
        if object_key:
            urls.append(_public_oss_url(object_key))
    return urls


# ---------------------------------------------------------------------------
# knowledge endpoints
# ---------------------------------------------------------------------------
