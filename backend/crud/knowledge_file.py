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


_CHINESE_NUMERAL = "一二三四五六七八九十百千万零〇两"
_PDF_PAGE_HEADING_RE = re.compile(r"^第\s*\d+\s*页$")
_MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_CHAPTER_HEADING_RE = re.compile(
    rf"^第[{_CHINESE_NUMERAL}0-9]+\s*[章节条款篇部分卷编].*"
)
_CHINESE_ORDER_HEADING_RE = re.compile(rf"^[{_CHINESE_NUMERAL}]+、\S+")
_PAREN_ORDER_HEADING_RE = re.compile(rf"^[（(][{_CHINESE_NUMERAL}0-9]+[）)]\S+")
_DECIMAL_HEADING_RE = re.compile(r"^\d+(?:\.\d+)*[\.．、)]\s*\S+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；;])")
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[，,、：:])")


def _normalize_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _looks_like_long_list_item(line: str) -> bool:
    stripped = _normalize_line(line)
    return len(stripped) > 48 and bool(_DECIMAL_HEADING_RE.match(stripped))


def _heading_level(line: str) -> int | None:
    stripped = _normalize_line(line)
    if not stripped or _looks_like_long_list_item(stripped):
        return None
    if _PDF_PAGE_HEADING_RE.match(stripped):
        return 0

    markdown_match = _MARKDOWN_HEADING_RE.match(stripped)
    if markdown_match:
        return min(len(markdown_match.group(1)), 4)

    if _CHAPTER_HEADING_RE.match(stripped):
        if re.match(rf"^第[{_CHINESE_NUMERAL}0-9]+\s*(?:章|篇|部|部分|卷|编)", stripped):
            return 1
        return 2
    if _CHINESE_ORDER_HEADING_RE.match(stripped):
        return 2
    if _PAREN_ORDER_HEADING_RE.match(stripped):
        return 3

    decimal_match = _DECIMAL_HEADING_RE.match(stripped)
    if decimal_match:
        if re.match(r"^\d+[、)]", stripped):
            return 3
        prefix = re.match(r"^(\d+(?:\.\d+)*)", stripped)
        if prefix:
            return min(prefix.group(1).count(".") + 2, 4)
        return 2

    return None


def _is_section_heading(line: str) -> bool:
    level = _heading_level(line)
    return level is not None and level > 0


def _is_page_heading(line: str) -> bool:
    return _heading_level(line) == 0


def _is_heading(line: str) -> bool:
    return _heading_level(line) is not None


def _split_long_segment(segment: str, max_len: int) -> list[str]:
    segment = _normalize_line(segment)
    if not segment:
        return []
    if len(segment) <= max_len:
        return [segment]

    pieces: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(segment):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= max_len:
            pieces.append(sentence)
            continue

        clause_buffer = ""
        for clause in _CLAUSE_SPLIT_RE.split(sentence):
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


def _semantic_units(block: str, max_len: int) -> list[str]:
    lines = [_normalize_line(line) for line in block.split("\n") if _normalize_line(line)]
    if not lines:
        return []
    if len(lines) == 1:
        return _split_long_segment(lines[0], max_len)

    units: list[str] = []
    body_buffer: list[str] = []

    def flush_body() -> None:
        nonlocal body_buffer
        if not body_buffer:
            return
        body = " ".join(body_buffer).strip()
        units.extend(_split_long_segment(body, max_len))
        body_buffer = []

    for line in lines:
        if _is_heading(line):
            flush_body()
            units.append(line)
        else:
            body_buffer.append(line)
    flush_body()
    return units


def _trim_heading_path(heading_path: dict[int, str], level: int) -> dict[int, str]:
    return {key: value for key, value in heading_path.items() if key < level}


def _heading_lines(heading_path: dict[int, str]) -> list[str]:
    return [heading_path[key] for key in sorted(heading_path)]


def _content_length(lines: list[str]) -> int:
    return len("\n".join(line for line in lines if line).strip())


def _overlap_units(body_lines: list[str], max_chars: int) -> list[str]:
    if max_chars <= 0:
        return []

    selected: list[str] = []
    total = 0
    for line in reversed(body_lines):
        if not line or _is_heading(line):
            continue
        candidate_len = len(line)
        if selected and total + candidate_len > max_chars:
            break
        selected.append(line)
        total += candidate_len
        if len(selected) >= 2:
            break
    return list(reversed(selected))


def chunk_text(text: str, file_id: int, chunk_size: int = 1200, chunk_overlap: int = 150) -> list[dict]:
    """Split text by heading hierarchy, with paragraph/sentence fallback for long sections."""
    text = "" if text is None else str(text)
    if not text.strip():
        return []

    normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")
    raw_blocks = [block.strip() for block in re.split(r"\n\s*\n+", normalized_text) if block.strip()]
    semantic_limit = max(chunk_size, 200)
    overlap_limit = max(chunk_overlap, 0)

    units: list[str] = []
    for block in raw_blocks:
        units.extend(_semantic_units(block, semantic_limit))

    chunks: list[dict] = []
    heading_path: dict[int, str] = {}
    current_body: list[str] = []
    pending_overlap: list[str] = []

    def flush_chunk() -> None:
        nonlocal current_body, pending_overlap
        if not current_body:
            return

        heading_lines = _heading_lines(heading_path)
        content_lines = []
        for line in [*heading_lines, *pending_overlap, *current_body]:
            if line and line not in content_lines:
                content_lines.append(line)

        chunk_text_value = "\n".join(content_lines).strip()
        if chunk_text_value:
            chunks.append({"id": f"{len(chunks)}", "text": chunk_text_value})
        pending_overlap = _overlap_units(current_body, overlap_limit)
        current_body = []

    for unit in units:
        level = _heading_level(unit)
        if level == 0:
            flush_chunk()
            pending_overlap = []
            continue

        if level is not None:
            flush_chunk()
            heading_path = _trim_heading_path(heading_path, level)
            heading_path[level] = unit
            pending_overlap = []
            continue

        projected_lines = [*_heading_lines(heading_path), *pending_overlap, *current_body, unit]
        if current_body and _content_length(projected_lines) > semantic_limit:
            flush_chunk()
        current_body.append(unit)

    flush_chunk()

    if not chunks and heading_path:
        heading_text = "\n".join(_heading_lines(heading_path)).strip()
        if heading_text:
            chunks.append({"id": "0", "text": heading_text})

    return chunks

