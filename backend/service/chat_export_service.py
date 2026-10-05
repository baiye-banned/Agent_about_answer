"""会话 Markdown 下载（issue #252）：鉴权后内部按主键分批读取。"""

from io import StringIO
import logging
import re
from urllib.parse import quote

from fastapi import Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from crud import chat as crud_chat
from database.session import get_db
from model.models import User
from service.auth_service import get_current_user
from service.json_utils import load_json_value


logger = logging.getLogger(__name__)
EXPORT_FAILED_MESSAGE = "导出会话失败，请稍后重试"
_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WINDOWS_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def export_filename(title: str | None) -> str:
    """生成不带路径、控制字符或 Windows 保留设备名的下载文件名。"""
    stem = _UNSAFE_FILENAME.sub("", title or "").strip().strip(".").strip()
    if not stem:
        stem = "conversation"
    if _WINDOWS_RESERVED.match(stem):
        stem = "_" + stem
    # 控制响应头与文件名长度；避免多字节中文标题超出常见文件系统的字节上限。
    stem = stem.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" .")
    return f"{stem or 'conversation'}.md"


def _source_text(value) -> str:
    # sources 存量 JSON 中可能有脏字段；不把嵌套对象中的任意数据整包序列化。
    return str(value) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def format_message_markdown(message) -> str:
    """保留正文原文和已保存的来源，仅输出用户可读的文件/片段字段。"""
    timestamp = message.created_at.isoformat() if message.created_at else ""
    parts = [f"## {message.role} · {timestamp}\n\n", message.content, "\n\n"]
    if message.role == "assistant":
        sources = load_json_value(message.sources, [])
        sources = [source for source in sources if isinstance(source, dict)]
        if sources:
            parts.append("### 参考来源\n\n")
        for source in sources:
            index = _source_text(source.get("index"))
            name = _source_text(source.get("file_name"))
            file_id = _source_text(source.get("file_id"))
            chunk_id = _source_text(source.get("chunk_id"))
            parts.append(
                f"- [{index}] {name}（文件 ID：{file_id}；片段 ID：{chunk_id}）\n\n"
            )
            # 新来源保存完整 content；兼容只有 excerpt 的历史来源。
            content = _source_text(source.get("content")) or _source_text(source.get("excerpt"))
            if content:
                parts.extend([content, "\n\n"])
    return "".join(parts)


def export_conversation(
    cid: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """先完成查库及格式化，成功后才返回文件；不会发送半份下载。"""
    try:
        conversation = crud_chat.get_conversation(db, cid, user.id)
        if conversation is None:
            raise HTTPException(404, "对话不存在")

        title = conversation.title or ""
        upper_id = crud_chat.get_message_export_upper_id(db, cid)
        with StringIO() as markdown:
            markdown.write(f"# {title}\n")
            if upper_id is not None:
                markdown.write("\n")
                after_id = None
                while True:
                    batch = crud_chat.list_message_export_batch(
                        db, cid, upper_id=upper_id, after_id=after_id
                    )
                    if not batch:
                        break
                    for message in batch:
                        markdown.write(format_message_markdown(message))
                    after_id = batch[-1].id
            content = markdown.getvalue()

        filename = quote(export_filename(title), safe="")
        return Response(
            content=content,
            media_type="text/markdown",
            headers={
                "Content-Disposition": f"attachment; filename=\"conversation.md\"; filename*=UTF-8''{filename}",
                "Cache-Control": "no-store",
            },
        )
    except HTTPException:
        raise
    except Exception:
        # 异常可能带数据库连接串等敏感数据，仅记录固定文案。
        logger.error("conversation Markdown export failed")
        raise HTTPException(500, EXPORT_FAILED_MESSAGE) from None
