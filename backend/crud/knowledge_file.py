from io import BytesIO
import re

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, load_only

from model.models import KnowledgeFile
from service.utils_service import KNOWLEDGE_UPLOAD_TYPE_ERROR_MESSAGE, _internal_error_detail


DOCX_PARSE_FAILED_MESSAGE = "DOCX 解析失败，请确认文件未损坏后重试"
PDF_PARSE_FAILED_MESSAGE = "PDF 解析失败，请确认文件未损坏后重试"


def serialize_knowledge_file(file_entry: KnowledgeFile) -> dict:
    return {
        "id": file_entry.id,
        "knowledge_base_id": file_entry.knowledge_base_id,
        "name": file_entry.name,
        "size": file_entry.size,
        "created_at": file_entry.created_at.isoformat() if file_entry.created_at else "",
    }


def list_knowledge_files(db: Session, knowledge_base_id: int, user_id: int) -> list[KnowledgeFile]:
    # 列表只渲染元数据，显式排除 content（LONGTEXT）：否则每列一个文件就把正文整列读进内存。
    # 调用方若再访问 file_entry.content，SQLAlchemy 会按行补查，本函数的返回值禁止用于正文读取。
    return (
        db.query(KnowledgeFile)
        .options(load_only(
            KnowledgeFile.id,
            KnowledgeFile.knowledge_base_id,
            KnowledgeFile.name,
            KnowledgeFile.size,
            KnowledgeFile.created_at,
        ))
        .filter_by(knowledge_base_id=knowledge_base_id, user_id=user_id)
        .order_by(KnowledgeFile.created_at.desc())
        .all()
    )


def get_knowledge_file(db: Session, fid: int, user_id: int) -> KnowledgeFile | None:
    return db.query(KnowledgeFile).filter_by(id=fid, user_id=user_id).first()


def create_knowledge_file(
    db: Session,
    *,
    knowledge_base_id: int,
    name: str,
    size: int,
    content: str,
    user_id: int,
) -> KnowledgeFile:
    entry = KnowledgeFile(
        knowledge_base_id=knowledge_base_id,
        name=name,
        size=size,
        content=content,
        user_id=user_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def delete_knowledge_file(db: Session, fid: int, user_id: int) -> KnowledgeFile | None:
    entry = get_knowledge_file(db, fid, user_id)
    if not entry:
        return None
    db.delete(entry)
    db.commit()
    return entry


def get_knowledge_content(db: Session, fid: int, user_id: int) -> dict | None:
    entry = get_knowledge_file(db, fid, user_id)
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
    """按扩展名抽取文本；白名单外的类型直接拒绝，不再兜底当纯文本解码。"""
    lower_name = (filename or "").lower()
    if lower_name.endswith(".docx"):
        return extract_docx_text(content)
    if lower_name.endswith(".pdf"):
        return extract_pdf_text(content)
    if lower_name.endswith(".txt") or lower_name.endswith(".md"):
        return content.decode("utf-8", errors="replace")
    raise HTTPException(400, KNOWLEDGE_UPLOAD_TYPE_ERROR_MESSAGE)


def extract_docx_text(content: bytes) -> str:
    from docx import Document

    try:
        document = Document(BytesIO(content))
    except Exception as exc:
        raise HTTPException(400, _internal_error_detail(DOCX_PARSE_FAILED_MESSAGE, "docx_parse", exc))
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
        raise HTTPException(400, _internal_error_detail(PDF_PARSE_FAILED_MESSAGE, "pdf_parse", exc))


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
_CHAPTER_MARKER_RE = re.compile(rf"^第[{_CHINESE_NUMERAL}0-9]+\s*[章节条款篇部分卷编]")
_CHAPTER_HEADING_RE = re.compile(
    rf"^第[{_CHINESE_NUMERAL}0-9]+\s*[章节条款篇部分卷编].*"
)
_CHINESE_ORDER_HEADING_RE = re.compile(rf"^[{_CHINESE_NUMERAL}]+、\S+")
_PAREN_ORDER_HEADING_RE = re.compile(rf"^[（(][{_CHINESE_NUMERAL}0-9]+[）)]\S+")
_DECIMAL_HEADING_RE = re.compile(r"^\d+(?:\.\d+)*[\.．、)]\s*\S+")
# 只匹配编号标记本身（不含标记之后的文本），用于区分「纯标题」与「标记后跟正文」。
_CHINESE_ORDER_MARKER_RE = re.compile(rf"^[{_CHINESE_NUMERAL}]+、")
_PAREN_ORDER_MARKER_RE = re.compile(rf"^[（(][{_CHINESE_NUMERAL}0-9]+[）)]")
_DECIMAL_MARKER_RE = re.compile(r"^\d+(?:\.\d+)*[\.．、)]")
# 四类编号标记：「第N条」「三、」「（一）」「1、/1.」，标记后是否跟正文共用同一套判定。
_ORDER_MARKER_RES = (
    _CHAPTER_MARKER_RE,
    _CHINESE_ORDER_MARKER_RE,
    _PAREN_ORDER_MARKER_RE,
    _DECIMAL_MARKER_RE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；;])")
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[，,、：:])")
# 标题行不会是一整句话，出现句末标点说明标记后面跟的是正文。
_SENTENCE_END_RE = re.compile(r"[。！？；;]")
# 超过该长度的整行无论如何都按正文处理。只服务于 _looks_like_long_list_item：
# 小数链会把标记正则整行吃光，只有「整行长度」这个信号还够得着它。
_LONG_HEADING_MAX_LEN = 48
# 编号标记的长度，中文序数按 4 位数字（「第一百二十三条」）再加一个分隔空格算。
# 它只决定「边界统一到哪一档」：标记不超过这个长度的前缀，判定边界一律是
# tail > _ORDER_HEADING_TAIL_MAX_LEN；更长的标记则回落到整行那条旧判据，只会更宽、
# 不会更严，因此不会丢正文。
_MAX_ORDER_MARKER_LEN = 8
# 标题不会写这么长：**标记之后**的文本超过该长度即按正文处理。
# 量在标记之后的文本上，而不是整行——标记本身长度随前缀不同
# （「三、」2 字 /「（一）」3 字 /「第十二条」4 字），量整行会让「标题 ↔ 正文」的
# 边界按前缀漂移，同一段正文换个编号方式就从保留变成丢弃（issue #83 第 2 项）。
# 取 _LONG_HEADING_MAX_LEN - _MAX_ORDER_MARKER_LEN 而不是直接复用 48：量到标记之后
# 总会让边界提前，提前得比最长标记还多，就会把旧口径下已经判成正文的行重新判回标题、
# 把正文丢掉（对抗评审实测：45~48 字无标点正文换了长标记后整批翻转）。
# 统一边界与「不比旧口径更严」由 _looks_like_order_heading_with_body 的两条判据
# 取或来共同保证：这里负责统一，整行那条负责兜住超出上述长度的标记。
_ORDER_HEADING_TAIL_MAX_LEN = _LONG_HEADING_MAX_LEN - _MAX_ORDER_MARKER_LEN


def _normalize_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _looks_like_long_list_item(line: str) -> bool:
    """小数编号**链**（「1.2.…20.1.」）按正文处理。

    不能并入 _looks_like_order_heading_with_body：_DECIMAL_MARKER_RE 是贪婪匹配，
    碰到整行只由数字与点号组成的行会把整行吃光、标记后为空，那条判定取不到文本、
    长度信号失效；而 _DECIMAL_HEADING_RE 能回溯到第一个点号后由 \\s*\\S+ 吃掉其余
    部分，仍然匹配。只有这里拦得住这种行。

    「标记吃光整行」是主判据，必须保留原有的长度保护作为兜底：深层小数编号的长行
    （「1.2.…30. 某某」）标记之后仍有文本，主判据够不着它，而 _DECIMAL_HEADING_RE
    仍会把它匹配成标题——只有整行长度拦得下。它不会重新造成前缀分裂：
    长度信号在 tail > _ORDER_HEADING_TAIL_MAX_LEN 之前不会先开火（标记 2 字 +
    tail ≤ 41 的整行远不到 48），两条判据给出同一边界。
    """
    stripped = _normalize_line(line)
    if not _DECIMAL_HEADING_RE.match(stripped):
        return False
    return not _order_heading_tail(stripped) or len(stripped) > _LONG_HEADING_MAX_LEN


def _order_heading_tail(line: str) -> str:
    """取编号标记（第N条 / 三、 / （一） / 1、）之后的剩余文本；为空说明是纯标题行。"""
    for marker_re in _ORDER_MARKER_RES:
        marker = marker_re.match(line)
        if marker:
            return line[marker.end():].strip()
    return ""


def _looks_like_order_heading_with_body(line: str) -> bool:
    """「编号 + 正文同行」排版：标记后跟着成句正文或标记后文本过长时，不能整行当标题丢弃。

    制度/法规类文档常把编号与正文写在同一行，这类行一旦被当成标题，
    正文就永远进不了 current_body，最终整篇只剩兜底的最后一行标题。
    issue #53 只挂进了「第N条」，这里把「三、」「（一）」「1、」「1.」四种前缀
    并入同一套判定，避免它们各自依赖长度阈值兜底。

    issue #83 第 2 项：长度信号量在标记之后的文本上（原先量整行），四类前缀的
    「标题 ↔ 正文」边界因此落在同一个字符数上，不再随标记本身的长短漂移。
    标记之后没有文本（纯标题行）的行仍走标题分支。

    单独的标记不构成「同行排版」判据：短标题与短正文在这一层无法区分，
    「三、员工迟到30分钟以内罚款50元，由人事部汇总」这类不带句末标点的短语正文
    仍然会被判成标题。整篇都判成标题时由 chunk_text 的结构兜底救回（issue #83 第 1 项）。
    """
    tail = _order_heading_tail(line)
    if not tail:
        return False
    # 整行那条判据必须留着：量到标记之后总会让边界提前，提前量取决于标记有多长，
    # 而标记长度没有上界（「第一百二十三条 」就是 8 字）。只按 tail 判，长标记的行
    # 会比旧口径更严——旧口径判正文的行被重新判回标题、正文丢掉（对抗评审实测到
    # 「第一百二十三条 + 41 字」这一例）。两条判据取或，等于「旧口径永远成立」，
    # 新口径只是在它之上再把边界统一提前到 _ORDER_HEADING_TAIL_MAX_LEN。
    if len(line) > _LONG_HEADING_MAX_LEN or len(tail) > _ORDER_HEADING_TAIL_MAX_LEN:
        return True
    return bool(_SENTENCE_END_RE.search(tail))


def _heading_level(line: str) -> int | None:
    stripped = _normalize_line(line)
    if not stripped or _looks_like_long_list_item(stripped):
        return None
    if _PDF_PAGE_HEADING_RE.match(stripped):
        return 0

    markdown_match = _MARKDOWN_HEADING_RE.match(stripped)
    if markdown_match:
        return min(len(markdown_match.group(1)), 4)

    # 编号标记后跟正文的行按正文处理：整行当标题会让正文整段进不了分块。
    if _looks_like_order_heading_with_body(stripped):
        return None

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

    def assemble(candidate_units: list[str], *, as_body: bool) -> tuple[list[dict], dict[int, str]]:
        """把候选单元按标题层级组装成切片。

        as_body=True 时所有单元一律当正文（结构兜底重跑用），此时不会产生标题路径。
        """
        chunks: list[dict] = []
        heading_path: dict[int, str] = {}
        current_body: list[str] = []
        pending_overlap: list[str] = []
        # 已经被写进过切片的标题**层位**（不是标题文本）。按层位记而不是按文本记：
        # 制度文档里同一行会重复出现（40 行同文语料很常见），按文本记会把「这一行
        # 进过库」误判成「这个位置进过库」，同一行只留一份、其余仍被丢掉。
        # 层位在重新赋值时清掉（见下方 set），flush 成功时把当时路径里的层位全记上。
        emitted_levels: set[int] = set()

        def flush_chunk() -> None:
            nonlocal current_body, pending_overlap
            if not current_body:
                return

            # 正文行必须逐条保留：同一句话在文档里重复出现（制度文档很常见）时去重，
            # 会让入库文本合计远小于原文，属于静默丢正文。
            # 标题不会在这里重复：进过 current_body 的标题只有 _rescue_dropped_headings
            # 收回来那一种，而它同时会从 heading_path 里摘掉，不会再当一次前缀；
            # _overlap_units 也会跳过标题行。
            heading_lines = _heading_lines(heading_path)
            content_lines = [line for line in [*heading_lines, *pending_overlap, *current_body] if line]

            chunk_text_value = "\n".join(content_lines).strip()
            if chunk_text_value:
                chunks.append({"id": f"{len(chunks)}", "text": chunk_text_value})
                emitted_levels.update(heading_path)
            pending_overlap = _overlap_units(current_body, overlap_limit)
            current_body = []

        def _rescue_dropped_headings(min_level: int | None) -> None:
            """把「从没进过任何切片、又马上要被覆盖掉」的标题行收回正文，独立成片。

            issue #83 第 1 项的返工（对抗评审 major-1）：编号短语正文行只占少数时
            （实测覆盖率 53.9%~80.9%），整篇覆盖率够不着 0.5 的兜底线，兜底与告警
            双双哑火，这些行仍然整行消失。这是形状相关的静默丢字，全局阈值救不了
            ——阈值调低会让正常文档整篇当正文重排、标题层级全丢。

            判据落在局部：一个标题若是**从没进过任何切片**（没当过一次标题前缀），
            紧接着又要被同层/更浅层标题覆盖，那它承载的文字除它自己以外没有任何
            去处，只能丢——这正是「三、员工迟到30分钟以内罚款50元，由人事部汇总」
            连排时的形态（前 N-1 行互相覆盖）。

            收回来时先从 heading_path 摘掉再成片：留着会让同一行既当标题前缀又当正文。
            成片时仍带上还没被覆盖的上级标题作前缀，位置就在原地，不打乱文档顺序。

            只在 flush 之后调用：真被带进过切片的层位都记在 emitted_levels 里，
            这里只回收确实没去处的那些，不会重复回收。
            """
            rescued: list[str] = []
            for key in sorted(heading_path):
                if min_level is not None and key < min_level:
                    continue
                if key in emitted_levels:
                    continue
                rescued.append(heading_path.pop(key))
            if not rescued:
                return
            current_body.extend(rescued)
            flush_chunk()

        for unit in candidate_units:
            level = None if as_body else _heading_level(unit)
            if level == 0:
                flush_chunk()
                pending_overlap = []
                continue

            if level is not None:
                flush_chunk()
                # flush 之后再判：走空的 flush 说明这些标题一个都没被带进切片。
                _rescue_dropped_headings(level)
                heading_path = _trim_heading_path(heading_path, level)
                heading_path[level] = unit
                # 换了新标题，这一层位「进过库」的记录必须作废，否则这一行被覆盖时
                # 会被误判成已经进过库而丢掉。
                emitted_levels.discard(level)
                pending_overlap = []
                continue

            projected_lines = [*_heading_lines(heading_path), *pending_overlap, *current_body, unit]
            if current_body and _content_length(projected_lines) > semantic_limit:
                flush_chunk()
            current_body.append(unit)

        flush_chunk()
        # 文档以标题收尾（后面再没有正文行）时收尾的 flush 不会带上它们，
        # 这些标题同样会整行消失——最后一次回收机会。
        _rescue_dropped_headings(None)
        return chunks, heading_path

    chunks, _heading_path = assemble(units, as_body=False)

    # 结构兜底（issue #83 第 1 项）：入库文本合计远低于原文，说明「标题」判定在这里
    # 把正文吃掉了，典型是「三、员工迟到30分钟以内罚款50元，由人事部汇总」这种
    # 编号与短语正文同行的排版——40 行语料修复前只剩兜底的最后一行，覆盖率 2.4%。
    #
    # 触发条件用已有的异常覆盖率口径，而不是「一行正文都没有」：真实制度文档常是
    # 若干编号行夹一两句正文，那种文档同样会丢（对抗评审实测 4.9%），
    # 只认「全篇皆标题」就漏掉了它们。整篇重新按正文组装，宁可标题层级不准
    # （正文会并进上一节、继承上一节标题），也不能整段丢正文。
    # 正常文档的覆盖率在 90% 以上，远高于这条线，分块结果不受影响。
    if units and chunk_coverage_ratio(text, chunks) < KNOWLEDGE_INDEX_MIN_COVERAGE_RATIO:
        chunks, _heading_path = assemble(units, as_body=True)

    return chunks


# 入库文本合计低于原文该比例即视为异常：分块规则可能把正文整段丢掉了。
KNOWLEDGE_INDEX_MIN_COVERAGE_RATIO = 0.5


def chunk_coverage_ratio(text: str, chunks: list[dict]) -> float:
    """分块结果相对原文的文本覆盖率，用于暴露「分块吞正文」这类静默丢失。"""
    source = "" if text is None else str(text)
    if not source:
        return 1.0
    covered = sum(len(chunk.get("text") or "") for chunk in chunks)
    return covered / len(source)

