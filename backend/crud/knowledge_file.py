from io import BytesIO
import re

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from model.models import KnowledgeFile


def serialize_knowledge_file(file_entry: KnowledgeFile) -> dict:
    return {
        "id": file_entry.id,
        "knowledge_base_id": file_entry.knowledge_base_id,
        "name": file_entry.name,
        "size": file_entry.size,
        "created_at": file_entry.created_at.isoformat() if file_entry.created_at else "",
    }


def list_knowledge_files(db: Session, knowledge_base_id: int) -> list[KnowledgeFile]:
    return (
        db.query(KnowledgeFile)
        .filter_by(knowledge_base_id=knowledge_base_id)
        .order_by(KnowledgeFile.created_at.desc())
        .all()
    )


def get_knowledge_file(db: Session, fid: int) -> KnowledgeFile | None:
    return db.query(KnowledgeFile).filter_by(id=fid).first()


def create_knowledge_file(
    db: Session,
    *,
    knowledge_base_id: int,
    name: str,
    size: int,
    content: str,
) -> KnowledgeFile:
    entry = KnowledgeFile(
        knowledge_base_id=knowledge_base_id,
        name=name,
        size=size,
        content=content,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def delete_knowledge_file(db: Session, fid: int) -> KnowledgeFile | None:
    entry = get_knowledge_file(db, fid)
    if not entry:
        return None
    db.delete(entry)
    db.commit()
    return entry


def get_knowledge_content(db: Session, fid: int) -> dict | None:
    entry = get_knowledge_file(db, fid)
    if not entry:
        return None
    content = entry.content or ""
    if not content and (entry.name or "").lower().endswith(".docx"):
        content = "该文件上传时未抽取内容，请重新上传以生成预览。"
    return {
        "id": entry.id,
        "name": entry.name,
        "content": content,
    }


def extract_file_text(filename: str, content: bytes) -> str:
    lower_name = filename.lower()
    if lower_name.endswith(".docx"):
        return extract_docx_text(content)
    if lower_name.endswith(".pdf"):
        return extract_pdf_text(content)
    return content.decode("utf-8", errors="replace")


def extract_docx_text(content: bytes) -> str:
    from docx import Document

    try:
        document = Document(BytesIO(content))
    except Exception as exc:
        raise HTTPException(400, f"DOCX 解析失败：{exc}")
    parts = [p.text.strip() for p in document.paragraphs if p.text.strip()]

    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))

    return "\n".join(parts)


def extract_pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise HTTPException(500, "后端缺少 pypdf 依赖，无法解析 PDF")

    try:
        reader = PdfReader(BytesIO(content), strict=False)
        parts = []
        for index, page in enumerate(reader.pages, start=1):
            page_text = (page.extract_text() or "").strip()
            if page_text:
                parts.append(f"第 {index} 页\n{page_text}")
        return "\n\n".join(parts)
    except Exception as exc:
        raise HTTPException(400, f"PDF 解析失败：{exc}")


def knowledge_file_save_error_message(exc: SQLAlchemyError) -> str:
    detail = str(exc)
    if "Incorrect string value" in detail or "1366" in detail:
        return (
            "文件内容包含中文字符，但当前 MySQL 表或字段仍不是 utf8mb4。"
            "请重启后端让启动迁移生效；如仍失败，请执行："
            "ALTER DATABASE rag_system CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; "
            "ALTER TABLE knowledge_files CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; "
            "ALTER TABLE knowledge_files MODIFY COLUMN content LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
        )
    return "文件信息写入数据库失败，请稍后重试"


_SECTION_HEADING_RE = re.compile(
    r"^(?:"
    r"[一二三四五六七八九十百千万零〇]+、"
    r"|第[一二三四五六七八九十百千万零〇0-9]+[章节条款篇]"
    r")"
)
_LIST_ITEM_RE = re.compile(r"^(?:[0-9]+|[一二三四五六七八九十百千万零〇]+)[、\.．)]")


def _normalize_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _is_section_heading(line: str) -> bool:
    stripped = _normalize_line(line)
    return bool(stripped) and len(stripped) <= 24 and bool(_SECTION_HEADING_RE.match(stripped))


def _is_list_item(line: str) -> bool:
    stripped = _normalize_line(line)
    return bool(stripped) and bool(_LIST_ITEM_RE.match(stripped))


def _split_long_segment(segment: str, max_len: int) -> list[str]:
    segment = _normalize_line(segment)
    if not segment:
        return []
    if len(segment) <= max_len:
        return [segment]

    pieces: list[str] = []
    for sentence in re.split(r"(?<=[。！？；;])", segment):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= max_len:
            pieces.append(sentence)
            continue

        clause_buffer = ""
        for clause in re.split(r"(?<=[，,、：:])", sentence):
            clause = clause.strip()
            if not clause:
                continue
            candidate = f"{clause_buffer}{clause}" if clause_buffer else clause
            if len(candidate) <= max_len:
                clause_buffer = candidate
                continue
            if clause_buffer:
                pieces.append(clause_buffer.strip())
                clause_buffer = ""
            if len(clause) <= max_len:
                clause_buffer = clause
            else:
                for start in range(0, len(clause), max_len):
                    tail = clause[start : start + max_len].strip()
                    if tail:
                        pieces.append(tail)
        if clause_buffer:
            pieces.append(clause_buffer.strip())

    return pieces


def chunk_text(text: str, file_id: int, chunk_size: int = 800, chunk_overlap: int = 50) -> list[dict]:
    """Split text into semantic chunks that prefer headings, paragraphs, and sentences."""
    text = "" if text is None else str(text)
    if not text.strip():
        return []

    normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")
    raw_blocks = [block.strip() for block in re.split(r"\n\s*\n+", normalized_text) if block.strip()]

    segments: list[str] = []
    for block in raw_blocks:
        block_lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not block_lines:
            continue
        if len(block_lines) == 1:
            segments.extend(_split_long_segment(block_lines[0], chunk_size))
            continue

        for line in block_lines:
            if _is_section_heading(line) or _is_list_item(line):
                segments.append(line)
            else:
                segments.extend(_split_long_segment(line, chunk_size))

    chunks: list[dict] = []
    current_lines: list[str] = []
    current_section: str = ""
    previous_tail: str = ""

    def flush_chunk() -> None:
        nonlocal current_lines, previous_tail
        if not current_lines:
            return
        chunk_text_value = "\n".join(current_lines).strip()
        if chunk_text_value:
            chunks.append({"id": f"{len(chunks)}", "text": chunk_text_value})
        tail_candidate = ""
        for candidate in reversed(current_lines):
            if candidate and not _is_section_heading(candidate):
                tail_candidate = candidate
                break
        previous_tail = tail_candidate if len(tail_candidate) <= chunk_overlap else ""
        current_lines = []

    for segment in segments:
        if _is_section_heading(segment):
            flush_chunk()
            current_section = segment
            current_lines = [segment]
            previous_tail = ""
            continue

        if not current_lines and current_section:
            current_lines.append(current_section)
            if previous_tail and previous_tail != current_section:
                current_lines.append(previous_tail)

        projected_length = len("\n".join(current_lines + [segment]).strip()) if current_lines else len(segment)
        if current_lines and projected_length > chunk_size:
            flush_chunk()
            current_lines = [current_section] if current_section else []
            if previous_tail and previous_tail != current_section and previous_tail != segment:
                current_lines.append(previous_tail)

        if not current_lines and current_section:
            current_lines = [current_section]

        if current_lines and current_lines[-1] == current_section and segment == current_section:
            continue
        current_lines.append(segment)

    flush_chunk()
    return chunks

